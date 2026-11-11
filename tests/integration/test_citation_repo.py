"""引文只追加；已知 ID 与晚到的唯一精确别名构成库内反向查询。"""

from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from threading import Barrier

import pytest
from sqlalchemy import func, select, update
from sqlalchemy.exc import IntegrityError

from litgleaner.domain.citation import Citation
from litgleaner.domain.identity import Alias
from litgleaner.domain.paper import Paper
from litgleaner.infra.db.migrate import upgrade_database
from litgleaner.infra.db.schema import citation, paper
from litgleaner.infra.db.store import SqlStore


@pytest.fixture
def store(tmp_path):
    url = f"sqlite:///{tmp_path / 'citation.db'}"
    upgrade_database(url)
    store = SqlStore(url)
    for paper_id in ("P1", "P2", "P3", "P4"):
        store.papers.insert(
            Paper(paper_id, f"Paper {paper_id}", paper_id, None, (), None, None, None, "t")
        )
    yield store
    store.dispose()


def reference(**changes):
    return replace(
        Citation(
            "P1",
            "api:0:hash",
            '  {"raw":"author\'s 引用"}\n  ',
            "api",
            0,
            None,
            "An unknown title",
            (Alias("doi", "10.1000/late"),),
            ("We extend the earlier method.", "原始引用上下文"),
            ("background", "method"),
            None,
            "t1",
        ),
        **changes,
    )


@pytest.mark.parametrize("influential", [None, False, True])
def test_roundtrip_preserves_raw_text_aliases_contexts_boolean_and_optional_position(
    store, influential
):
    item = reference(influential=influential, page=0, bbox=(10, 20, 300, 40))
    assert store.citations.add(citation=item)
    (loaded,) = store.citations.outgoing("P1")
    assert loaded == item
    assert isinstance(loaded.cited_aliases, tuple) and isinstance(loaded.cited_aliases[0], Alias)
    assert isinstance(loaded.contexts, tuple) and isinstance(loaded.intents, tuple)
    assert loaded.influential is influential and loaded.bbox == (10, 20, 300, 40)
    assert store.citations.outgoing("missing") == store.citations.incoming("missing") == ()


def test_duplicate_key_keeps_first_fact_and_new_content_key_keeps_another_version(store):
    first = reference()
    assert store.citations.add(first) and not store.citations.add(first)
    changed = replace(
        first,
        raw_text="replacement",
        source="changed",
        source_position=9,
        cited_paper_id="P2",
        contexts=(),
        influential=True,
        page=7,
        created_at="t9",
    )
    assert not store.citations.add(changed)
    assert store.citations.outgoing("P1") == (first,)
    assert store.citations.incoming("P2") == ()
    new_version = replace(changed, position="changed:9:different-hash")
    assert store.citations.add(new_version)
    assert set(store.citations.outgoing("P1")) == {first, new_version}


def test_raw_update_cannot_overwrite_reference_or_contexts(store):
    item = reference()
    store.citations.add(item)
    with pytest.raises(IntegrityError, match="citation_fact_immutable"):
        store._db.write(update(citation).values(raw_text="rewritten", contexts="[]"))
    assert store.citations.outgoing("P1") == (item,)


def test_nullable_positions_contexts_and_unknown_references_are_retained(store):
    item = reference(cited_title=None, cited_aliases=(), contexts=(), intents=(), raw_text="")
    assert store.citations.add(item)
    assert store.citations.outgoing("P1") == (item,)
    assert store.citations.incoming("P2") == ()
    assert store._db.fetch(select(func.count()).select_from(paper))[0][0] == 4


@pytest.mark.parametrize("changes", [{"citing_paper_id": "missing"}, {"cited_paper_id": "missing"}])
def test_known_paper_links_are_protected_by_foreign_keys(store, changes):
    with pytest.raises(IntegrityError, match="FOREIGN KEY"):
        store.citations.add(reference(**changes))
    assert store.citations.outgoing("P1") == ()


def test_outgoing_sorts_source_then_numeric_source_position_then_content_key(store):
    rows = [
        reference(source=source, source_position=ordinal, position=key)
        for source, ordinal, key in (
            ("b", 0, "b:0:x"),
            ("a", 10, "a:10:z"),
            ("a", 2, "a:2:x"),
            ("a", 10, "a:10:a"),
        )
    ]
    for item in rows:
        store.citations.add(item)
    assert store.citations.outgoing("P1") == tuple(
        sorted(
            rows,
            key=lambda item: (
                item.source,
                item.source_position,
                item.position,
            ),
        )
    )


def test_late_paper_and_alias_resolve_incoming_without_updating_stored_fact(store):
    item = reference()
    store.citations.add(item)
    assert store.citations.incoming("late") == ()
    store.papers.insert(
        Paper("late", "Unrelated title", "unrelated", None, (), None, None, None, "t2")
    )
    assert store.citations.incoming("late") == ()
    store.papers.add_aliases("late", (Alias("doi", "10.1000/late"),))
    assert store.citations.incoming("late") == (item,)
    assert store.citations.outgoing("P1") == (item,)
    assert store.citations.incoming("late")[0].cited_paper_id is None


def test_title_is_never_used_to_guess_or_create_a_paper(store):
    item = reference(cited_title="Paper P2", cited_aliases=())
    store.citations.add(item)
    assert store.citations.incoming("P2") == ()
    assert store._db.fetch(select(func.count()).select_from(paper))[0][0] == 4


def test_alias_matching_is_exact_in_both_scheme_and_value(store):
    store.papers.add_aliases("P2", (Alias("doi", "10.1000/exact"),))
    rows = [
        reference(position=f"api:{index}:h", cited_aliases=(alias,))
        for index, alias in enumerate(
            (
                Alias("DOI", "10.1000/exact"),
                Alias("doi", "10.1000/EXACT"),
                Alias("doi", "10.1000/exact-extra"),
            )
        )
    ]
    for item in rows:
        store.citations.add(item)
    assert store.citations.incoming("P2") == ()
    exact = reference(position="api:9:h", cited_aliases=(Alias("doi", "10.1000/exact"),))
    store.citations.add(exact)
    assert store.citations.incoming("P2") == (exact,)


def test_multiple_aliases_to_same_paper_return_one_hit_but_conflicts_return_none(store):
    aliases = (Alias("doi", "10.1000/late"), Alias("arxiv", "2601.00001"))
    item = reference(cited_aliases=(*aliases, aliases[0], Alias("doi", "unknown")))
    store.citations.add(item)
    store.papers.add_aliases("P2", aliases)
    assert store.citations.incoming("P2") == (item,)
    second = reference(position="api:1:conflict", cited_aliases=(aliases[0], Alias("doi", "other")))
    store.citations.add(second)
    store.papers.add_aliases("P3", (Alias("doi", "other"),))
    assert store.citations.incoming("P2") == (item,)
    assert store.citations.incoming("P3") == ()


def test_recorded_target_remains_authoritative_when_aliases_point_elsewhere(store):
    item = reference(cited_paper_id="P4")
    store.citations.add(item)
    store.papers.add_aliases("P2", item.cited_aliases)
    assert store.citations.incoming("P4") == (item,)
    assert store.citations.incoming("P2") == ()


def test_paper_scoped_keys_keep_distinct_edges_and_incoming_order_stable(store):
    first = reference(cited_paper_id="P2")
    third = replace(first, citing_paper_id="P3")
    store.citations.add(third)
    store.citations.add(first)
    assert store.citations.incoming("P2") == (first, third)
    assert store.citations.outgoing("P1") == (first,) and store.citations.outgoing("P3") == (third,)


def test_parallel_same_key_writes_are_idempotent_without_overwriting_winner(store):
    rows = [reference(raw_text=f"Original variant {index}") for index in range(4)]
    start = Barrier(len(rows))

    def add(item):
        start.wait(timeout=5)
        return store.citations.add(item)

    with ThreadPoolExecutor(max_workers=len(rows)) as pool:
        assert sum(pool.map(add, rows)) == 1
    (winner,) = store.citations.outgoing("P1")
    assert winner in rows
    assert not store.citations.add(reference(raw_text="late replacement"))
    assert store.citations.outgoing("P1") == (winner,)


def test_atomic_rollback_removes_reference_and_alias_link_visibility_together(store):
    item = reference()
    with pytest.raises(RuntimeError), store.atomic():
        store.citations.add(item)
        store.papers.add_aliases("P2", item.cited_aliases)
        assert store.citations.incoming("P2") == (item,)
        raise RuntimeError("rollback")
    assert store.citations.outgoing("P1") == store.citations.incoming("P2") == ()
    assert store.papers.find_ids_by_aliases(item.cited_aliases) == frozenset()
