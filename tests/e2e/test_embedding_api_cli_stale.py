"""向量 CLI、只读 API 与全历史过期报告的真实入口合同。"""

import json
import struct
from dataclasses import asdict, replace

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import delete, insert, update
from typer.testing import CliRunner

from litgleaner import cli_library
from litgleaner.application import embeddings, stale
from litgleaner.application.embedding_inputs import inputs
from litgleaner.cli import app
from litgleaner.cli_shared import run_lock
from litgleaner.domain.embedding import PaperEmbedding
from litgleaner.domain.errors import ConfigError
from litgleaner.infra.config import load_config
from litgleaner.infra.db.schema import paper, paper_embedding, reading_state
from litgleaner.server.app import create_app
from litgleaner.server.security import LocalSecurity
from tests.e2e.embedding_helpers import setup
from tests.e2e.memory_helpers import add, extract, no_extract_route


def _rows(harness):
    return tuple(item for item in stale.report(harness.ctx).items if item.kind == "embedding")


def test_preview_similar_and_stale_are_fixed_batch_reads_with_no_external_actions(
    harness, monkeypatch
):
    model = setup(harness)
    a = add(harness, "a")
    add(harness, "b")
    embeddings.reembed(harness.ctx)
    db, queries = harness.ctx.store._db, []
    fetch = db.fetch

    def captured(statement):
        queries.append(statement)
        return fetch(statement)

    def forbidden(*_args, **_kwargs):
        raise AssertionError("readonly query invoked an external action or database write")

    monkeypatch.setattr(db, "fetch", captured)
    monkeypatch.setattr(db, "write", forbidden)
    monkeypatch.setattr(model, "embed", forbidden)
    monkeypatch.setattr(type(harness.ctx.llm), "call", forbidden)
    monkeypatch.setattr(type(harness.ctx.blobs), "put", forbidden)
    for source in harness.ctx.sources.values():
        monkeypatch.setattr(type(source), "fetch", forbidden)
    assert embeddings.preview(harness.ctx).cached == 2 and len(queries) == 4
    assert embeddings.similar(harness.ctx, a).total == 1 and len(queries) == 8
    assert len(_rows(harness)) == 2 and len(queries) == 25
    assert all(query.is_select for query in queries)


def test_historical_vectors_report_input_config_producer_and_current_selection(harness):
    model = setup(harness)
    a = add(harness, "a")
    embeddings.reembed(harness.ctx)
    (first,) = _rows(harness)
    assert first.status == "current" and first.selected
    harness.ctx.store._db.write(update(paper).where(paper.c.id == a).values(abstract="changed"))
    (old,) = _rows(harness)
    assert old.status == "stale" and not old.selected and "输入" in old.reasons[0]
    embeddings.reembed(harness.ctx)
    rows = _rows(harness)
    assert len(rows) == 2 and sum(item.selected for item in rows) == 1
    model.model = replace(model.model, producer_version="producer-new")
    assert all(item.status == "stale" and not item.selected for item in _rows(harness))
    assert any("运行库" in reason for item in _rows(harness) for reason in item.reasons)
    model.model = replace(model.model, available=False)
    assert all(item.status == "stale" and item.unchecked for item in _rows(harness))
    model.model = replace(model.model, configuration_version="new-config", model="other")
    assert any("配置" in reason for item in _rows(harness) for reason in item.reasons)


@pytest.mark.parametrize("retain_producer", [False, True])
def test_unavailable_model_is_unknown_without_fabricated_input_staleness(harness, retain_producer):
    model = setup(harness)
    add(harness, "a")
    embeddings.reembed(harness.ctx)
    version = model.model.producer_version if retain_producer else None
    model.model = replace(model.model, producer_version=version, available=False)
    (row,) = _rows(harness)
    assert row.status == "unknown" and row.unchecked and not row.reasons and not row.selected
    harness.ctx = replace(harness.ctx, embedder=None)
    (row,) = _rows(harness)
    assert row.status == "unknown" and not row.reasons


def test_history_outside_current_library_stays_current_but_is_not_selected(harness):
    setup(harness)
    a = add(harness, "a")
    embeddings.reembed(harness.ctx)
    harness.ctx.store._db.write(delete(reading_state).where(reading_state.c.paper_id == a))
    (row,) = _rows(harness)
    assert row.status == "current" and not row.selected
    assert embeddings.preview(harness.ctx).total == 0


@pytest.mark.parametrize("use_extraction", [True, False])
def test_unchecked_extraction_producer_affects_only_actually_used_sources(harness, use_extraction):
    setup(harness)
    if not use_extraction:
        harness.ctx = replace(
            harness.ctx,
            settings=replace(
                harness.ctx.settings,
                embedding=harness.ctx.settings.embedding.model_copy(
                    update={"max_input_chars": 100}
                ),
            ),
        )
    a = add(harness, "a" if use_extraction else "A" * 120)
    add(harness, "b")
    extract(harness, a, core=None, data={"problem": {"text": "Existing problem"}})
    embeddings.reembed(harness.ctx)
    no_extract_route(harness)
    row = next(item for item in _rows(harness) if item.paper_id == a)
    assert row.status == ("unknown" if use_extraction else "current")
    assert bool(row.unchecked) is use_extraction
    assert row.selected  # 候选查询实际仍选用该向量，完整核验状态另列。
    assert embeddings.similar(harness.ctx, a).total == 1


@pytest.mark.parametrize("corruption", ["vector", "metadata"])
def test_corrupt_stored_current_key_is_ignored_and_never_overwritten(harness, corruption):
    model = setup(harness)
    a = add(harness, "a")
    snapshot = harness.ctx.store.embeddings.snapshot()
    source = inputs(harness.ctx, snapshot.papers, snapshot.fulltexts, snapshot.extractions)[a]
    record = PaperEmbedding(
        a,
        source.input_hash,
        model.model.producer_version,
        model.model.configuration_version,
        model.model.model,
        3,
        "wrong actual text" if corruption == "metadata" else source.input_text,
        struct.pack("<3f", float("nan") if corruption == "vector" else 1, 0, 0),
        None,
        harness.ctx.now_iso(),
    )
    # 模拟老版本/外部写入的坏行，仍满足 SQLite 的字节与长度约束。
    harness.ctx.store._db.write(insert(paper_embedding).values(**asdict(record)))
    result = embeddings.similar(harness.ctx, a)
    assert result.matches == () and any(w.code.startswith("invalid_") for w in result.warnings)
    with pytest.raises(ConfigError, match="损坏"):
        embeddings.reembed(harness.ctx)
    assert not model.calls and harness.ctx.store.embeddings.for_paper(a) == (record,)
    assert _rows(harness)[0].status == "stale"


def test_new_readonly_endpoint_has_session_host_origin_and_limit_guards(harness):
    model = setup(harness)
    a = add(harness, "a")
    add(harness, "b")
    embeddings.reembed(harness.ctx)
    web = create_app(harness.ctx, LocalSecurity(48143, launch_token="embedding-test"), None)
    base = f"/api/papers/{a}/memory-similar"
    with TestClient(web, base_url="http://127.0.0.1:48143") as client:
        assert client.get(base).status_code == 401
        client.get("/launch", params={"token": "embedding-test"})
        response = client.get(base)
        assert response.status_code == 200 and response.json()["total"] == 1
        assert response.json()["matches"][0]["cosine"] == 1
        for limit in (0, -1, 201, "invalid"):
            assert client.get(base, params={"limit": limit}).status_code == 422
        assert client.get(base, headers={"Host": "other.test"}).status_code == 400
        assert client.get(base, headers={"Origin": "https://other.test"}).status_code == 403
        assert client.get("/api/papers/missing/memory-similar").status_code == 404
    assert len(model.calls) == 1


def test_real_cli_defaults_to_preview_executes_explicitly_and_filters_stale(harness, monkeypatch):
    model = setup(harness)
    add(harness, "a")
    monkeypatch.setattr(cli_library, "context_or_exit", lambda _: harness.ctx)
    args = ["--config", str(harness.root / "config")]
    runner = CliRunner()
    preview = runner.invoke(app, ["memory", "reembed", *args])
    assert preview.exit_code == 0, preview.output
    assert len(json.loads(preview.stdout)["pending_paper_ids"]) == 1 and not model.calls
    computed = runner.invoke(app, ["memory", "reembed", "--execute", *args])
    assert computed.exit_code == 0, computed.output
    assert json.loads(computed.stdout)["created"] == 1 and len(model.calls) == 1
    report = runner.invoke(app, ["stale", "--format", "json", "--kind", "embedding", *args])
    assert report.exit_code == 0, report.output
    assert json.loads(report.stdout)["current"] == 1
    assert json.loads(report.stdout)["items"][0]["kind"] == "embedding"


def test_cli_execute_honors_shared_run_lock_and_disabled_diagnostic(harness, monkeypatch):
    model = setup(harness)
    add(harness, "a")
    monkeypatch.setattr(cli_library, "context_or_exit", lambda _: harness.ctx)
    config = harness.root / "config"
    args = ["memory", "reembed", "--execute", "--config", str(config)]
    with run_lock(load_config(config)):
        refused = CliRunner().invoke(app, args)
    assert refused.exit_code == 3 and not model.calls
    harness.ctx = replace(harness.ctx, embedder=None)
    disabled = CliRunner().invoke(app, args)
    assert disabled.exit_code != 0 and "关闭" in disabled.output
