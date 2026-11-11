"""论文事实入库、去重补空及手工添加的同一条链路。"""

import json
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from threading import Barrier

import pytest
from sqlalchemy import text
from tests.e2e.conftest import _write_config

from litgleaner.application import inbox
from litgleaner.application.stages.discover import DiscoverStage, resolve_record
from litgleaner.bootstrap import build_context
from litgleaner.domain.identity import Alias
from litgleaner.domain.paper import Paper, SourceRecord
from litgleaner.domain.query import IdentifierLookup
from litgleaner.infra.config import load_config
from litgleaner.infra.db.migrate import upgrade_database
from litgleaner.infra.db.store import SqlStore


@pytest.fixture
def store(tmp_path):
    url = f"sqlite:///{tmp_path / 'facts.db'}"
    upgrade_database(url)
    result = SqlStore(url)
    yield result
    result.dispose()


def _paper(**facts):
    return Paper("P", "Original", "original", None, (), None, None, None, "t", **facts)


def _record(**facts):
    return SourceRecord(title="Original", doi="10.1000/original", **facts)


def test_paper_facts_roundtrip_on_get_and_title_lookup(store):
    expected = _paper(publication_types=("Review", "Meta-Analysis"), citation_count=0)
    store.papers.insert(expected)
    assert store.papers.get("P") == expected
    assert store.papers.find_by_title_key("original") == (expected,)


def test_alias_match_fills_only_missing_metadata_then_preserves_first_facts(store):
    paper_id = resolve_record(store.papers, _record(), "t")
    richer = _record(publication_types=("Review",), citation_count=0, abstract="Known abstract")
    assert resolve_record(store.papers, richer, "t2") == paper_id
    before = store.papers.get(paper_id)
    changed = _record(publication_types=("Article",), citation_count=100, abstract="Changed")
    assert resolve_record(store.papers, changed, "t3") == paper_id
    assert resolve_record(store.papers, _record(), "t4") == paper_id
    assert store.papers.get(paper_id) == before
    assert (before.publication_types, before.citation_count) == (("Review",), 0)
    assert before.abstract == "Known abstract"


def test_title_author_match_carries_facts_and_new_alias_without_new_paper(store):
    common = {"title": "A Long Original Paper Title", "authors": ("Ada Lovelace",)}
    old = SourceRecord(**common, arxiv_id="1907.10485")
    paper_id = resolve_record(store.papers, old, "t")
    richer = SourceRecord(**common, doi="10.1000/new", publication_types=("Article",))
    assert resolve_record(store.papers, richer, "t2") == paper_id
    assert store.papers.get(paper_id).publication_types == ("Article",)
    assert store.papers.find_ids_by_aliases((Alias("doi", "10.1000/new"),)) == {paper_id}


def test_fill_empty_array_once_without_erasing_known_count_or_emptying_types(store):
    store.papers.insert(_paper(citation_count=12))
    store._db.write(text("UPDATE paper SET publication_types='[ ]' WHERE id='P'"))
    store.papers.fill_missing("P", {"publication_types": "[]", "citation_count": 0})
    assert store.papers.get("P").publication_types == ()
    store.papers.fill_missing("P", {"publication_types": '["Review"]'})
    store.papers.fill_missing("P", {"publication_types": "[]", "citation_count": None})
    assert store.papers.get("P") == _paper(publication_types=("Review",), citation_count=12)


def test_concurrent_alias_resolution_reuses_one_paper_and_first_nonempty_types(store):
    paper_id = resolve_record(store.papers, _record(), "t")
    barrier = Barrier(2)

    def enrich(kind):
        barrier.wait(timeout=5)
        with store.atomic():
            return resolve_record(store.papers, _record(publication_types=(kind,)), "later")

    with ThreadPoolExecutor(max_workers=2) as pool:
        assert list(pool.map(enrich, ("Review", "Article"))) == [paper_id, paper_id]
    assert store.papers.get(paper_id).publication_types in (("Review",), ("Article",))
    assert len(store.papers.find_by_title_key("original")) == 1


def test_conflicting_aliases_do_not_write_facts_to_either_candidate(store):
    first = resolve_record(store.papers, _record(), "t")
    other = replace(_paper(), id="other", title="Another", title_key="another")
    store.papers.insert(other)
    store.papers.add_aliases("other", (Alias("arxiv", "1907.10485"),))
    conflict = _record(arxiv_id="1907.10485", publication_types=("Review",), citation_count=3)
    resolve_record(store.papers, conflict, "t2")
    assert store.papers.get(first).publication_types == store.papers.get("other").publication_types
    assert (
        store.papers.get(first).citation_count is store.papers.get("other").citation_count is None
    )


@pytest.fixture
def ctx(tmp_path):
    _write_config(tmp_path)
    config = load_config(tmp_path / "config")
    upgrade_database(config.database_url)
    context = build_context(config)
    (tmp_path / "config/demo/papers.json").write_text(
        json.dumps(
            [
                {
                    "title": "Source original",
                    "doi": "10.1000/original",
                    "publication_types": ["Review"],
                    "citation_count": 42,
                }
            ]
        ),
        encoding="utf-8",
    )
    yield context
    context.store.dispose()


@pytest.mark.parametrize("method", ["identifier", "pdf"])
def test_inbox_preserves_source_facts_and_offline_duplicate_does_not_erase_them(ctx, method):
    if method == "identifier":
        item = inbox.add_identifier(ctx, "10.1000/original")
    else:
        item = inbox.add_pdf(ctx, "paper.pdf", b"%PDF-1.7\n", "10.1000/original")
    assert item.status == "done"
    before = ctx.store.papers.get(item.paper_id)
    assert (before.publication_types, before.citation_count) == (("Review",), 42)
    offline = replace(ctx, sources={})
    duplicate = inbox.add_identifier(offline, "10.1000/original")
    assert duplicate.paper_id == item.paper_id
    assert ctx.store.papers.get(item.paper_id) == before


def test_discover_preserves_source_facts_and_archives_original_response(ctx, tmp_path):
    query = IdentifierLookup(doi="10.1000/original")
    payload = {"source": "alpha", "query": query.model_dump()}
    DiscoverStage().process(ctx, payload)
    paper_id = next(iter(ctx.store.papers.find_ids_by_aliases((Alias("doi", query.doi),))))
    assert ctx.store.papers.get(paper_id).publication_types == ("Review",)
    assert ctx.store.papers.get(paper_id).citation_count == 42
    assert len(tuple(tmp_path.rglob("*.zst"))) == 1
