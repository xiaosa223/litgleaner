"""真实 API、零写读取及过期来源不能影响候选分数。"""

from dataclasses import replace

import pytest
import yaml
from fastapi.testclient import TestClient

from litgleaner.adapters.embeddings import fastembed as adapter
from litgleaner.application import embeddings, memory_candidates
from litgleaner.domain.errors import ConfigError, InvalidRequest, NotFound
from litgleaner.domain.memory_policy import MemoryPolicy
from litgleaner.infra.config import load_config
from litgleaner.server.app import create_app
from litgleaner.server.security import LocalSecurity
from tests.e2e.embedding_helpers import setup
from tests.e2e.memory_helpers import add, citation, extract, no_extract_route
from tests.e2e.test_memory_candidates import abstract, policy


def test_fixed_ten_selects_and_no_index_refresh_archive_sdk_or_model_action(harness, monkeypatch):
    model = setup(harness)
    a, b = add(harness, "anchor"), add(harness, "neighbor")
    abstract(harness, a, "rarepapertoken")
    abstract(harness, b, "rarepapertoken")
    embeddings.reembed(harness.ctx)
    db, queries = harness.ctx.store._db, []
    original = db.fetch

    def capture(statement):
        queries.append(statement)
        return original(statement)

    def forbidden(*_args, **_kwargs):
        raise AssertionError("candidate query performed I/O, model action or database write")

    monkeypatch.setattr(db, "fetch", capture)
    monkeypatch.setattr(db, "write", forbidden)
    monkeypatch.setattr(adapter, "_sdk", forbidden)
    monkeypatch.setattr(model, "embed", forbidden)
    monkeypatch.setattr(harness.ctx.store, "memory_snapshot", forbidden)
    monkeypatch.setattr(harness.ctx.store.embeddings, "snapshot", forbidden)
    monkeypatch.setattr(harness.ctx.store.search, "pending_documents", forbidden)
    monkeypatch.setattr(type(harness.ctx.blobs), "get", forbidden)
    monkeypatch.setattr(type(harness.ctx.llm), "call", forbidden)
    for source in harness.ctx.sources.values():
        monkeypatch.setattr(type(source), "fetch", forbidden)
    assert memory_candidates.candidates(harness.ctx, a).total == 1
    assert len(queries) == 10


@pytest.mark.parametrize("change", ["input", "producer", "language", "document"])
def test_stale_extraction_drops_shared_facts_and_fts_before_normalization(harness, change):
    a, b, c = (add(harness, name) for name in ("anchor", "neighbor", "third"))
    abstract(harness, a, "rarepapertoken")
    abstract(harness, c, "rarepapertoken")
    # b 的 FTS 只能从当前提取命中，不能由 metadata 命中。
    extract(harness, a)
    kwargs = {"input_hash": "old"} if change == "input" else {}
    kwargs |= {"producer": "old"} if change == "producer" else {}
    extract(harness, b, data={"problem": {"text": "rarepapertoken " * 40}}, **kwargs)
    if change == "language":
        harness.ctx = replace(
            harness.ctx, settings=replace(harness.ctx.settings, reading_language="en")
        )
    if change == "document":
        harness.ctx.store.papers.save_fulltext(
            replace(harness.ctx.store.papers.get_fulltext(b), doc_key="blob://changed"),
            harness.ctx.now_iso(),
        )
    result = memory_candidates.candidates(harness.ctx, a)
    assert [match.facts.paper.paper_id for match in result.matches] == [c]
    assert result.matches[0].signals.text == 1


def test_current_extraction_fts_survives_missing_route_with_explicit_unchecked_warning(harness):
    a, b = add(harness, "anchor"), add(harness, "neighbor")
    abstract(harness, a, "rarepapertoken")
    latest = extract(harness, b, data={"problem": {"text": "rarepapertoken"}}, core=None)
    no_extract_route(harness)
    result = memory_candidates.candidates(harness.ctx, a)
    assert result.matches[0].text_hits[0].source_id == str(latest.id)
    assert result.matches[0].text_hits[0].kind == "extraction"
    assert any(w.paper_id == b and w.code == "producer_unchecked" for w in result.warnings)


@pytest.mark.parametrize("data", [[], {"method": 1, "datasets": "old"}])
def test_legacy_extraction_shapes_do_not_crash_candidate_or_reintroduce_old_facts(harness, data):
    a, b = add(harness, "anchor"), add(harness, "neighbor")
    extract(harness, a, data=data, core=None)
    citation(harness, a, target=b)
    result = memory_candidates.candidates(harness.ctx, a)
    assert result.matches[0].score.total == 11
    assert any(w.code == "legacy_fields" for w in result.warnings)


def test_weight_change_reorders_same_evidence_without_writes(harness, monkeypatch):
    a, b, c = (add(harness, name) for name in ("anchor", "neighbor", "third"))
    citation(harness, a, target=b)
    extract(harness, a)
    extract(harness, c)
    before = memory_candidates.candidates(harness.ctx, a)
    assert [item.facts.paper.paper_id for item in before.matches] == [b, c]
    weights = {**MemoryPolicy().weights.model_dump(), "core_concepts": 20}
    policy(harness, weights=weights)

    def forbidden(*_args, **_kwargs):
        raise AssertionError("weight change wrote to database")

    monkeypatch.setattr(harness.ctx.store._db, "write", forbidden)
    after = memory_candidates.candidates(harness.ctx, a)
    assert [item.facts.paper.paper_id for item in after.matches] == [c, b]
    assert before.policy_hash != after.policy_hash


@pytest.mark.parametrize("condition", ["unavailable", "stale", "missing"])
def test_missing_or_stale_vector_signal_does_not_block_other_candidates(harness, condition):
    model = setup(harness)
    a, b = add(harness, "anchor"), add(harness, "neighbor")
    citation(harness, a, target=b)
    if condition != "missing":
        embeddings.reembed(harness.ctx)
    if condition == "unavailable":
        model.model = replace(model.model, producer_version=None, available=False)
    elif condition == "stale":
        abstract(harness, b, "New actual input")
    result = memory_candidates.candidates(harness.ctx, a)
    (match,) = result.matches
    assert match.score.total == 11
    assert match.signals.cosine is None and match.vector is None


def test_api_session_host_origin_scope_limit_and_complete_response(harness):
    a, b = add(harness, "anchor"), add(harness, "neighbor")
    citation(harness, a, target=b)
    outside = add(harness, "outside", library=False)
    base = f"/api/papers/{a}/memory-candidates"
    web = create_app(harness.ctx, LocalSecurity(48145, launch_token="candidate-test"), None)
    with TestClient(web, base_url="http://127.0.0.1:48145") as client:
        assert client.get(base).status_code == 401
        client.get("/launch", params={"token": "candidate-test"})
        found = client.get(base)
        assert found.status_code == 200 and found.json()["total"] == 1
        assert found.json()["matches"][0]["score"]["total"] == 11
        assert found.json()["policy_hash"] == MemoryPolicy().policy_hash
        for limit in (0, -1, 201, "invalid"):
            assert client.get(base, params={"limit": limit}).status_code == 422
        assert client.get(base, headers={"Host": "other.test"}).status_code == 400
        assert client.get(base, headers={"Origin": "https://other.test"}).status_code == 403
        assert client.get(f"/api/papers/{outside}/memory-candidates").status_code == 404
        assert client.get("/api/papers/missing/memory-candidates").status_code == 404
    for limit in (0, -1, 201, True):
        with pytest.raises(InvalidRequest):
            memory_candidates.candidates(harness.ctx, a, limit=limit)
    with pytest.raises(NotFound):
        memory_candidates.candidates(harness.ctx, outside)


def test_candidate_configuration_is_loaded_and_does_not_change_embedding_identity(harness):
    first = harness.rebuild()
    data = {"candidates": {"max_pairs": 7, "cosine_min": 0.7, "weights": {"text": 0}}}
    path = harness.root / "config/memory.yaml"
    path.write_text(yaml.safe_dump(data))
    loaded = harness.rebuild()
    assert loaded.settings.memory_policy.max_pairs == 7
    assert loaded.settings.memory_policy.cosine_min == 0.7
    assert loaded.settings.memory_policy.weights.text == 0
    assert loaded.embedder.configuration_version == first.embedder.configuration_version
    assert loaded.settings.memory_policy.policy_hash != first.settings.memory_policy.policy_hash


@pytest.mark.parametrize(
    "changes", [{"max_pairs": True}, {"weights": {"text": -1}}, {"unknown": 1}]
)
def test_candidate_config_errors_name_the_source_file(harness, changes):
    path = harness.root / "config/memory.yaml"
    path.write_text(yaml.safe_dump({"candidates": changes}))
    with pytest.raises(ConfigError, match=r"memory\.yaml"):
        load_config(path.parent)
