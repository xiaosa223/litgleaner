"""全局概念的精确复用、追加事实、课题筛选与有来源的 FTS 检索。"""

import json
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from pathlib import Path
from threading import Barrier

import pytest
from sqlalchemy import insert, select, update
from sqlalchemy.exc import IntegrityError

from litgleaner.domain.knowledge import ConceptOccurrence
from litgleaner.domain.paper import Paper
from litgleaner.infra.db.migrate import upgrade_database
from litgleaner.infra.db.schema import (
    concept,
    concept_alias,
    concept_occurrence,
    digest,
    digest_item,
    extraction,
)
from litgleaner.infra.db.store import SqlStore
from litgleaner.ports.store import TopicState, TriageResult


@pytest.fixture
def store(tmp_path: Path):
    url = f"sqlite:///{tmp_path / 'knowledge.db'}"
    upgrade_database(url)
    store = SqlStore(url)
    for paper_id in ("P1", "P2", "P3"):
        store.papers.insert(
            Paper(paper_id, f"Paper {paper_id}", paper_id, None, (), None, None, None, "t")
        )
    yield store
    store.dispose()


def occurrence(concept_id, **changes):
    return replace(
        ConceptOccurrence(
            concept_id,
            "P1",
            "core",
            "A verified definition of the spectral method.",
            2,
            (10, 20, 30, 40),
            True,
            "input-v1",
            "producer-v1",
            "t1",
        ),
        **changes,
    )


def _link(store, paper_id, topic_id, *, topic_hash="current", relevant=True):
    store.judgments.save_topic_state(TopicState(topic_id, "active", "current", "t"), "t")
    store.judgments.save_triage(
        TriageResult(paper_id, topic_id, topic_hash, relevant, "reason", "v1", "test"),
        "t",
    )


def test_normalization_reuses_first_raw_canonical_without_semantic_merging(store):
    repo = store.knowledge
    original = "  Ｓｔｒａße\u00a0\nNetwork  "
    first = repo.register(original, None, "t1")
    for name in ("strasse network", "STRASSE  NETWORK", "Straße\tNetwork"):
        assert repo.register(name, "en", "t2") == first
        assert repo.resolve(name) == first
    assert repo.get(first.id) == first and first.name == original and first.created_at == "t1"
    assert len(first.id) == 26 and len(repo.aliases(first.id)) == 1
    assert repo.aliases(first.id)[0].language is None
    singular = repo.register("Graph", "en", "t1")
    plural = repo.register("Graphs", "en", "t1")
    assert singular.id != plural.id and repo.count(None, "") == 3


def test_aliases_are_exact_unique_immutable_mappings_with_optional_language(store):
    repo = store.knowledge
    first, second = repo.register("Spectral Method", "en", "t1"), repo.register("Other", None, "t1")
    assert repo.add_alias(first.id, "谱方法", "zh-CN", "t2")
    assert not repo.add_alias(first.id, " 谱方法 ", None, "t3")
    assert repo.register("谱方法", None, "t4") == first
    with pytest.raises(ValueError, match=r"^concept_alias_conflict$"):
        repo.add_alias(second.id, "谱方法", "zh-CN", "t5")
    alias = next(item for item in repo.aliases(first.id) if item.name == "谱方法")
    assert (alias.normalized_name, alias.concept_id, alias.language, alias.created_at) == (
        "谱方法",
        first.id,
        "zh-CN",
        "t2",
    )
    assert [item.normalized_name for item in repo.aliases(first.id)] == [
        "spectral method",
        "谱方法",
    ]


def test_empty_names_and_unknown_records_have_explicit_behavior(store):
    repo = store.knowledge
    assert repo.get("missing") is repo.resolve("  ") is None
    assert repo.aliases("missing") == repo.occurrences("missing") == repo.for_paper("missing") == ()
    for name in ("", "\t\n\u3000"):
        with pytest.raises(ValueError, match=r"^concept_name_empty$"):
            repo.register(name, None, "t")
        with pytest.raises(ValueError, match=r"^concept_name_empty$"):
            repo.add_alias("missing", name, None, "t")
    assert repo.list(None, "", 50, 0) == () and repo.count(None, "") == 0


def test_parallel_normalized_registration_creates_one_concept_and_one_alias(store):
    names = ["ＡＩ  Model", "ai model", " AI\tMODEL ", "Ai\nModel"]
    start = Barrier(len(names))

    def register(name):
        start.wait(timeout=5)
        return store.knowledge.register(name, None, "t")

    with ThreadPoolExecutor(max_workers=len(names)) as pool:
        registered = list(pool.map(register, names))
    assert len({item.id for item in registered}) == 1
    assert store.knowledge.count(None, "") == 1
    assert len(store.knowledge.aliases(registered[0].id)) == 1


def test_parallel_alias_registration_cannot_move_the_winning_mapping(store):
    repo = store.knowledge
    concepts = [repo.register(name, None, "t") for name in ("First", "Second")]
    start = Barrier(2)

    def add(item):
        start.wait(timeout=5)
        try:
            return repo.add_alias(item.id, "Shared Alias", None, "t")
        except ValueError as error:
            assert str(error) == "concept_alias_conflict"
            return "conflict"

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(add, concepts))
    assert results.count(True) == results.count("conflict") == 1
    assert repo.resolve("shared alias").id in {item.id for item in concepts}
    assert sum(len(repo.aliases(item.id)) for item in concepts) == 3


def test_occurrence_roundtrip_and_duplicate_writes_keep_first_complete_fact(store):
    repo = store.knowledge
    item = repo.register("Spectral Method", None, "t")
    first = occurrence(item.id, page=0)
    assert repo.add_occurrence(first) and not repo.add_occurrence(first)
    changed = replace(
        first,
        role="mentioned",
        definition=None,
        page=7,
        bbox=None,
        verified=False,
        input_hash="new-hash",
        producer_version="new",
        created_at="t9",
    )
    assert not repo.add_occurrence(changed)
    assert repo.occurrences(item.id) == repo.for_paper("P1") == (first,)
    assert isinstance(repo.occurrences(item.id)[0].bbox, tuple)
    second = replace(first, paper_id="P2", bbox=None, created_at="t2")
    assert repo.add_occurrence(second) and repo.occurrences(item.id) == (first, second)


def test_foreign_keys_reject_alias_and_occurrence_for_unknown_records(store):
    with pytest.raises(IntegrityError, match="FOREIGN KEY"):
        store.knowledge.add_alias("missing", "alias", None, "t")
    item = store.knowledge.register("Known", None, "t")
    for invalid in (occurrence("missing"), occurrence(item.id, paper_id="missing")):
        with pytest.raises(IntegrityError, match="FOREIGN KEY"):
            store.knowledge.add_occurrence(invalid)


@pytest.mark.parametrize(
    "changes",
    [
        {"role": "invented"},
        {"page": -1},
        {"page": 1.5},
        {"definition": "bad unverified definition", "verified": False},
        {"definition": None, "verified": True},
        {"definition": "short", "verified": True},
    ],
)
def test_invalid_occurrence_is_rejected_even_when_primary_key_already_exists(store, changes):
    item = store.knowledge.register("Known", None, "t")
    first = occurrence(item.id)
    store.knowledge.add_occurrence(first)
    with pytest.raises(IntegrityError, match="CHECK"):
        store.knowledge.add_occurrence(replace(first, **changes))
    assert store.knowledge.occurrences(item.id) == (first,)


@pytest.mark.parametrize("table", [concept, concept_alias, concept_occurrence])
def test_facts_cannot_be_updated_directly(store, table):
    item = store.knowledge.register("Known", None, "t")
    store.knowledge.add_occurrence(occurrence(item.id))
    with pytest.raises(IntegrityError, match="knowledge_fact_immutable"):
        store._db.write(update(table).values(created_at="overwritten"))
    assert store.knowledge.get(item.id).created_at == "t"
    assert store.knowledge.occurrences(item.id)[0].created_at == "t1"


def test_search_name_alias_and_verified_definition_with_real_pages_and_author_origin(store):
    repo = store.knowledge
    item = repo.register("Spectral Method", "en", "t")
    repo.add_alias(item.id, "谱方法", "zh-CN", "t")
    repo.add_occurrence(occurrence(item.id))
    repo.add_occurrence(occurrence(item.id, paper_id="P2", verified=False, definition=None, page=5))
    for query in ("spectral",):
        hits = store.search.find(query, 20)
        assert {(hit.kind, hit.id, hit.paper_id, hit.page, hit.authorship) for hit in hits} == {
            ("concept", item.id, "P1", 2, "author"),
            ("concept", item.id, "P2", 5, "author"),
        }
        assert all("谱方法" not in hit.text for hit in hits)
    assert store.search.find("谱方法", 20) == ()
    (hit,) = store.search.find("verified definition", 20)
    assert hit.paper_id == "P1" and hit.authorship == "author"
    assert repo.list(None, "verified definition", 20, 0) == (item,)
    assert repo.list(None, "谱方", 20, 0) == (item,)
    assert repo.count(None, "谱方法") == 1
    assert repo.count(None, "谱方法 verified definition") == 1


def test_alias_added_after_occurrences_refreshes_private_lookup_without_author_misattribution(
    store,
):
    repo = store.knowledge
    item = repo.register("Graph Model", None, "t")
    repo.add_occurrence(occurrence(item.id))
    repo.add_occurrence(occurrence(item.id, paper_id="P2"))
    assert store.search.find("graphical nickname", 20) == ()
    repo.add_alias(item.id, "Graphical Nickname", "en", "t2")
    assert store.search.find("graphical nickname", 20) == ()
    assert {hit.paper_id for hit in store.search.find("graph model", 20)} == {"P1", "P2"}
    assert repo.count(None, "graphical nickname") == 1
    assert len(store._db.fetch(select(concept_alias))) == 2


def test_unlocated_concept_is_list_searchable_but_never_a_global_paper_hit(store):
    item = store.knowledge.register("Bare Concept", None, "t")
    assert store.knowledge.list(None, "bare", 20, 0) == (item,)
    assert store.knowledge.count(None, "bare") == 1
    assert store.search.find("bare", 20) == ()
    assert store.knowledge.list("unknown", "", 20, 0) == ()


def test_unverified_definition_stays_in_extraction_without_entering_fact_search(store):
    repo = store.knowledge
    item = repo.register("Known Name", None, "t")
    raw = json.dumps(
        {"key_concepts": [{"name": item.name, "definition": "Fictional unverified evidence"}]}
    )
    store._db.write(
        insert(extraction).values(
            paper_id="P1",
            input_hash="h",
            extractor_version="v",
            data=raw,
            created_at="t",
        )
    )
    missing = occurrence(item.id, verified=False, definition=None)
    assert repo.add_occurrence(missing) and repo.for_paper("P1") == (missing,)
    assert repo.count(None, "fictional") == 0 and store.search.find("fictional", 20) == ()
    assert store._db.fetch(select(extraction.c.data))[0].data == raw


def test_topic_filter_uses_any_current_relevant_or_historical_paper_and_deduplicates(store):
    repo = store.knowledge
    shared, other = repo.register("Shared", None, "t"), repo.register("Other", None, "t")
    for paper_id in ("P1", "P2"):
        repo.add_occurrence(occurrence(shared.id, paper_id=paper_id))
        _link(store, paper_id, "alpha")
    repo.add_occurrence(occurrence(other.id, paper_id="P3"))
    _link(store, "P3", "alpha", relevant=False)
    _link(store, "P3", "old", topic_hash="outdated")
    store._db.write(
        insert(digest).values(
            id="D",
            route_id="r",
            topic_id="historical",
            channel="c",
            period="p",
            created_at="t",
        )
    )
    store._db.write(insert(digest_item).values(digest_id="D", position=0, paper_id="P3", card="{}"))
    assert repo.list("alpha", "", 20, 0) == (shared,) and repo.count("alpha", "") == 1
    assert repo.list("historical", "", 20, 0) == (other,)
    assert repo.list("old", "", 20, 0) == ()
    assert repo.list("unknown", "", 20, 0) == repo.list("", "", 20, 0) == ()
    assert repo.count("unknown", "") == repo.count("", "") == 0


def test_list_count_order_pagination_and_literal_queries_are_consistent(store):
    repo = store.knowledge
    created = [repo.register(name, None, "t") for name in ("Zeta", "Alpha", "Beta")]
    ordered = tuple(sorted(created, key=lambda item: (item.name, item.id)))
    assert repo.list(None, "", 2, 0) == ordered[:2]
    assert repo.list(None, "", 2, 2) == ordered[2:] and repo.count(None, "") == 3
    assert repo.list(None, "", 2, 3) == repo.list(None, "", 0, 0) == ()
    for query in ("' OR 1=1 --", '" NEAR(*)', "%_", "*", "unknown"):
        assert repo.list(None, query, 20, 0) == () and repo.count(None, query) == 0
    assert repo.list(None, '"Alpha"', 20, 0) == (ordered[0],)


def test_registration_and_occurrence_rollback_removes_fts_projection_together(store):
    with pytest.raises(RuntimeError), store.atomic():
        item = store.knowledge.register("Transient Knowledge", None, "t")
        store.knowledge.add_occurrence(occurrence(item.id))
        raise RuntimeError("rollback")
    assert store.knowledge.resolve("transient knowledge") is None
    assert store.knowledge.count(None, "transient") == 0
    assert store.search.find("transient", 20) == ()
