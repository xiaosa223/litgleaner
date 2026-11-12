"""真实SQLite全库词法与当前文库本地向量混排；无外部调用。"""

from dataclasses import replace

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import text

from litgleaner.application import embeddings, search, search_hybrid
from litgleaner.domain.hashing import stable_hash
from litgleaner.server.app import create_app
from litgleaner.server.security import LocalSecurity
from tests.e2e.search_hybrid_helpers import block_external, cache_vectors, setup
from tests.e2e.search_normalization_helpers import corpus, document, paper


def test_semantic_only_recall_returns_real_paper_metadata_with_current_input_hash(
    harness, monkeypatch
):
    model, (id_,) = setup(harness)
    abstract = "A distinct mechanism provides an experimental explanation."
    harness.ctx.store._db.write(
        text("UPDATE paper SET abstract=:abstract WHERE id=:id").bindparams(
            abstract=abstract, id=id_
        )
    )
    embeddings.reembed(harness.ctx)
    model.calls.clear()
    assert harness.ctx.store.search.find("survival", 30) == ()
    block_external(harness, monkeypatch)
    result = search.search(harness.ctx, "survival")
    assert result.mode == "hybrid" and result.protocol == "global-search-hybrid-v1"
    assert [
        (row.kind, row.id, row.paper_id, row.title, row.page, row.authorship) for row in result.hits
    ] == [("paper", id_, id_, "Unrelated mechanisms", None, "author")]
    assert result.signals[0].bm25 is None and result.signals[0].cosine == 1
    assert result.signals[0].text_contribution == 0 and result.signals[0].input_hash
    assert result.hits[0].text == abstract
    assert model.calls == [(("survival",), False)] and harness.llm_calls() == 0


def test_all_global_lexical_source_kinds_survive_without_getting_paper_cosine(harness):
    corpus(harness)
    outsider = paper(harness, "Outside ＳＴＲＡＳＳＥ", library=False)
    model, _ = setup(harness)
    # The existing corpus paper is already in the library, so reembed caches it too.
    lexical = harness.ctx.store.search.find("strasse", 100)
    result = search.search(harness.ctx, "STRASSE", limit=100)
    assert {(hit.kind, hit.paper_id, hit.id) for hit in lexical} <= {
        (hit.kind, hit.paper_id, hit.id) for hit in result.hits
    }
    assert outsider in {hit.paper_id for hit in result.hits}
    assert {hit.kind for hit in result.hits} >= {
        "paper",
        "fulltext",
        "note",
        "highlight",
        "extraction",
        "qa",
        "concept",
    }
    for row in result.signals:
        if row.kind != "paper" or row.paper_id == outsider:
            assert row.cosine is None and row.cosine_contribution == 0 and row.input_hash is None
    assert len(model.calls) == 1


def test_original_query_is_nfkc_whitespace_normalized_without_casefold_and_truncated(harness):
    model, _ = setup(harness)
    harness.ctx = replace(
        harness.ctx,
        settings=replace(
            harness.ctx.settings,
            embedding=harness.ctx.settings.embedding.model_copy(update={"max_input_chars": 100}),
        ),
    )
    query = "  ＳＴＲＡＳＳＥ\tCafe\u0301 " + "X" * 120
    result = search.search(harness.ctx, query)
    actual = ("STRASSE Café " + "X" * 120)[:100]
    assert model.calls == [((actual,), False)]
    assert result.query_hash == stable_hash(actual)


@pytest.mark.parametrize("state", ["no_cache", "unavailable", "disabled", "no_terms", "stale"])
def test_missing_current_cache_or_ready_model_never_runs_inference(harness, state):
    model, (id_,) = setup(harness, cache=state != "no_cache")
    query = "missing"
    if state == "unavailable":
        model.model = replace(model.model, available=False)
    elif state == "disabled":
        harness.ctx = replace(harness.ctx, embedder=None)
    elif state == "no_terms":
        query = "!!!"
    elif state == "stale":
        harness.ctx.store._db.write(
            text("UPDATE paper SET title='Changed title' WHERE id=:id").bindparams(id=id_)
        )
    result = search.search(harness.ctx, query)
    assert result.mode == "lexical" and result.hits == harness.ctx.store.search.find(query, 30)
    assert model.calls == [] and result.warnings


@pytest.mark.parametrize(
    "invalid", ["raise", "zero", "nan", "dimension", "count", "batch_identity"]
)
def test_invalid_query_vectors_restore_exact_lexical_order_and_excerpt(harness, invalid):
    model, _ = setup(harness, names=("Strasse original", "A strasse second paper"))
    model.invalid = invalid
    expected = harness.ctx.store.search.find("strasse", 30)
    result = search.search(harness.ctx, "strasse")
    assert result.mode == "lexical" and result.hits == expected
    assert result.warnings and "private/model/file" not in str(result.warnings)
    assert model.calls == [(("strasse",), False)]


@pytest.mark.parametrize("change_at,call_count", [(2, 0), (3, 0), (4, 1), (5, 1)])
def test_model_identity_races_before_during_and_after_fusion_fall_back(
    harness, change_at, call_count
):
    model, _ = setup(harness, names=("Strasse study",))
    model.change_at = change_at
    expected = harness.ctx.store.search.find("strasse", 30)
    result = search.search(harness.ctx, "strasse")
    assert result.mode == "lexical" and result.hits == expected and result.warnings
    assert len(model.calls) == call_count


def test_corrupt_one_cache_is_excluded_while_other_current_cache_still_recalls(harness):
    model, (bad, good) = setup(harness, names=("Bad unrelated", "Good unrelated"), cache=False)
    cache_vectors(harness, {bad: bytes(12)})
    result = search.search(harness.ctx, "survival")
    assert result.mode == "hybrid" and [row.paper_id for row in result.hits] == [good]
    assert any(bad in warning for warning in result.warnings) and len(model.calls) == 1


def test_negative_or_orthogonal_cache_is_not_semantic_only_result(harness):
    _, (negative, orthogonal, positive) = setup(
        harness, names=("Neg study", "Orth study", "Pos study"), cache=False
    )
    cache_vectors(harness, {negative: (-1.0, 0.0, 0.0), orthogonal: (0.0, 1.0, 0.0)})
    assert [row.paper_id for row in search.search(harness.ctx, "survival").hits] == [positive]


def test_snapshot_and_refresh_remain_six_selects_no_blob_read_or_writes(harness, monkeypatch):
    model, (id_,) = setup(harness)
    document(harness, id_, "Archive already indexed", 4)
    embeddings.reembed(harness.ctx)
    model.calls.clear()
    db, counts = harness.ctx.store._db, []
    fetch = db.fetch

    def counted(statement):
        counts.append(db._active is not None)
        return fetch(statement)

    block_external(harness, monkeypatch)
    monkeypatch.setattr(db, "fetch", counted)
    monkeypatch.setattr(harness.ctx.blobs, "get", lambda *_: pytest.fail("indexed archive reread"))
    result = search.search(harness.ctx, "survival")
    assert result.mode == "hybrid" and len(model.calls) == 1
    assert counts == [False, True, True, True, True, True]


def test_warning_output_is_bounded_even_when_many_caches_are_corrupt(harness):
    model, ids = setup(harness, names=tuple(f"Study{i}" for i in range(36)), cache=False)
    cache_vectors(harness, {id_: bytes(12) for id_ in ids[:-1]})
    result = search.search(harness.ctx, "survival")
    assert result.mode == "hybrid" and len(result.warnings) == 33
    assert "省略" in result.warnings[-1] and len(model.calls) == 1


def test_only_returned_ranks_map_original_text_into_excerpts(harness, monkeypatch):
    setup(harness, names=tuple(f"Long original study {i}" for i in range(40)))
    original, calls = search_hybrid.search_excerpt, []

    def counted(content, terms):
        calls.append(content)
        return original(content, terms)

    monkeypatch.setattr(search_hybrid, "search_excerpt", counted)
    result = search.search(harness.ctx, "study", limit=3)
    assert result.mode == "hybrid" and len(result.hits) == len(result.signals) == 3
    assert len(calls) == 3


def test_duplicate_fts_identity_merges_only_in_hybrid_and_fallback_preserves_duplicates(harness):
    model, (id_,) = setup(harness, names=("Needle paper",))
    harness.ctx.store._db.write(
        text(
            "INSERT INTO memory_fts(kind,id,paper_id,page,authorship,source_version,title,text,"
            "search_title,search_text) SELECT kind,id,paper_id,page,authorship,source_version,"
            "title,"
            "text,search_title,search_text FROM memory_fts WHERE kind='paper' AND paper_id=:id"
        ).bindparams(id=id_)
    )
    expected = harness.ctx.store.search.find("needle", 30)
    assert len(expected) == 2
    combined = search.search(harness.ctx, "needle")
    assert combined.mode == "hybrid" and len(combined.hits) == len(combined.signals) == 1
    model.invalid = "nan"
    fallback = search.search(harness.ctx, "needle")
    assert fallback.mode == "lexical" and fallback.hits == expected


def test_only_corrupt_cache_keeps_lexical_results_without_query_inference(harness):
    model, (id_,) = setup(harness, names=("Needle paper",), cache=False)
    cache_vectors(harness, {id_: bytes(12)})
    expected = harness.ctx.store.search.find("needle", 30)
    result = search.search(harness.ctx, "needle")
    assert result.mode == "lexical" and result.hits == expected
    assert model.calls == [] and any(id_ in warning for warning in result.warnings)


def test_api_exposes_explainable_signals_and_preserves_session_and_limit_security(harness):
    setup(harness)
    security = LocalSecurity(48127, launch_token="search-session")
    with TestClient(
        create_app(harness.ctx, security, None), base_url="http://127.0.0.1:48127"
    ) as client:
        assert client.get("/api/search", params={"q": "survival"}).status_code == 401
        client.get("/launch", params={"token": "search-session"}, follow_redirects=False)
        response = client.get("/api/search", params={"q": "survival"})
        assert response.status_code == 200
        data = response.json()
        assert data["mode"] == "hybrid" and data["signals"][0]["cosine_contribution"] == 1
        assert data["hits"][0]["authorship"] == "author" and data["query_hash"]
        assert client.get("/api/search", params={"q": "survival", "limit": 101}).status_code == 422
        assert client.get("/api/search", headers={"Host": "evil.test"}).status_code == 400
        assert client.get("/api/search", headers={"Origin": "http://evil.test"}).status_code == 403
