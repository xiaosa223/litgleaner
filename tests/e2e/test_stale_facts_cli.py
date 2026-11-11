"""统一报告覆盖全部事实历史与评分策略，且命令只读、零模型费用。"""

import json
import socket
from dataclasses import replace

import pytest
from typer.testing import CliRunner

from litgleaner import cli_library
from litgleaner.application.codec import compress_json, decompress_json
from litgleaner.application.llm_gateway import Prompt
from litgleaner.application.stale import report
from litgleaner.cli import app
from tests.e2e.conftest import Harness, paper

TOPIC = "alpha-topic"


@pytest.fixture
def judged(harness: Harness):
    harness.write_papers([paper("Read alpha", 5), paper("Other alpha", 4)])
    assert harness.run().ok
    return harness


def entries(result, kind):
    return tuple(entry for entry in result.items if entry.kind == kind)


def test_current_facts_and_incomplete_legacy_producers_are_distinct(judged):
    before = judged.llm_calls()
    result = report(judged.ctx)
    assert result.total == 6 and result.current == 2 and result.unknown == 4 and result.stale == 0
    assert all(
        entry.status == "current" and entry.selected for entry in entries(result, "extraction")
    )
    for kind in ("assessment", "score"):
        assert all(entry.status == "unknown" and entry.selected for entry in entries(result, kind))
        assert all("完整指纹" in " ".join(entry.unchecked) for entry in entries(result, kind))
    assert judged.llm_calls() == before


@pytest.mark.parametrize(
    "change",
    [
        "language",
        "budget",
        "topic",
        "semantic",
        "policy",
        "extract_prompt",
        "assess_prompt",
        "response_alias",
    ],
)
def test_fingerprints_and_policy_have_separate_failure_reasons(judged, change):
    ctx = judged.ctx
    if change == "language":
        ctx = replace(ctx, settings=replace(ctx.settings, reading_language="en"))
    elif change == "budget":
        ctx = replace(ctx, settings=replace(ctx.settings, assess_token_budget=7000))
    elif change == "topic":
        ctx = replace(
            ctx, topics={TOPIC: ctx.topics[TOPIC].model_copy(update={"background": "new"})}
        )
    elif change in ("semantic", "policy"):
        rubric = ctx.rubrics["default"]
        dims = tuple(
            dim.model_copy(
                update=(
                    {"weight": dim.weight + (1 if i == 0 else -1 if i == 1 else 0)}
                    if change == "policy"
                    else {"question": dim.question + " new"}
                )
            )
            for i, dim in enumerate(rubric.dimensions)
        )
        ctx = replace(ctx, rubrics={"default": rubric.model_copy(update={"dimensions": dims})})
    else:
        task = "extract" if change == "extract_prompt" else "assess"
        route = ctx.llm.routes[task]
        update = (
            {"model": "requested-alias"}
            if change == "response_alias"
            else {"prompt": Prompt(task, route.prompt.text + " changed")}
        )
        ctx = replace(
            ctx, llm=replace(ctx.llm, routes={**ctx.llm.routes, task: replace(route, **update)})
        )
    before = judged.llm_calls()
    result = report(ctx)
    extracted, assessed, scored = (
        entries(result, kind) for kind in ("extraction", "assessment", "score")
    )
    assert all(
        entry.status
        == ("stale" if change in ("language", "budget", "extract_prompt") else "current")
        for entry in extracted
    )
    assert all(
        entry.status
        == (
            "stale"
            if change in ("language", "budget", "topic", "semantic", "assess_prompt")
            else "unknown"
        )
        for entry in assessed
    )
    assert all(
        entry.status == ("unknown" if change in ("extract_prompt", "response_alias") else "stale")
        for entry in scored
    )
    if change == "policy":
        assert all("零费用重算" in " ".join(entry.reasons) for entry in scored)
    if change == "assess_prompt":
        assert all(entry.selected for entry in scored)  # 仍是现有评分视图选中的行；失效状态单独报告
    assert judged.llm_calls() == before


def test_all_extraction_history_is_checked_independently_of_latest_selection(judged):
    paper_id = judged.ctx.store.export_snapshot().papers[0].paper.id
    saved = judged.ctx.store.papers.get_extraction(paper_id)
    judged.ctx.store.papers.insert_extraction(
        paper_id, saved.input_hash, "legacy-producer", {}, "t"
    )
    result = entries(report(judged.ctx), "extraction")
    mine = tuple(entry for entry in result if entry.paper_id == paper_id)
    assert len(mine) == 2
    assert mine[0].status == "current" and mine[0].selected is False
    assert mine[1].status == "stale" and mine[1].selected is True


def test_missing_models_or_removed_topics_are_unknown_without_reprocessing(judged):
    ctx = replace(judged.ctx, llm=replace(judged.ctx.llm, routes={}))
    result = report(ctx)
    assert result.stale == 0 and result.unknown == 6 and result.current == 0
    removed = report(replace(ctx, topics={}))
    assert all(
        entry.status == "unknown" and entry.selected is False
        for kind in ("assessment", "score")
        for entry in entries(removed, kind)
    )


def test_missing_or_corrupt_documents_only_affect_associated_results_and_read_once(
    judged, monkeypatch
):
    snapshot = judged.ctx.store.stale_snapshot()
    broken_id = snapshot.papers[0].id
    broken_key = snapshot.fulltexts[broken_id].doc_key
    original = judged.ctx.blobs.get
    reads = []

    def get(key):
        reads.append(key)
        return b"corrupt archive" if key == broken_key else original(key)

    monkeypatch.setattr(judged.ctx.blobs, "get", get)
    result = report(judged.ctx)
    broken = [entry for entry in result.items if entry.paper_id == broken_id]
    assert all(entry.status == "unknown" for entry in broken)
    assert all("归档" in " ".join(entry.unchecked) for entry in broken)
    assert len(entries(result, "extraction")) == 2
    assert sum(entry.status == "current" for entry in entries(result, "extraction")) == 1
    assert len(reads) == len(set(reads)) == 2


def test_report_and_cli_never_write_or_query_after_snapshot_or_call_external_work(
    judged, monkeypatch
):
    calls, original = judged.llm_calls(), judged.ctx.store.stale_snapshot
    frozen = original()

    def forbidden(*_a, **_kw):
        pytest.fail("stale report performed extra database or external work")

    monkeypatch.setattr(judged.ctx.store, "stale_snapshot", lambda: frozen)
    monkeypatch.setattr(judged.ctx.store._db, "fetch", forbidden)
    monkeypatch.setattr(judged.ctx.store._db, "write", forbidden)
    monkeypatch.setattr(judged.llm, "complete", forbidden)
    monkeypatch.setattr(judged.llm, "stream", forbidden)
    monkeypatch.setattr(judged.ctx.blobs, "put", forbidden)
    monkeypatch.setattr(socket, "create_connection", forbidden)
    monkeypatch.setattr(cli_library, "context_or_exit", lambda _: judged.ctx)
    assert report(judged.ctx).total == 6
    response = CliRunner().invoke(app, ["stale", "--format", "json"])
    assert response.exit_code == 0, response.output
    result = json.loads(response.stdout)
    assert result["total"] == 6 and len(result["items"]) == 6
    assert result["stale"] == 0 and result["unknown"] == 4
    assert calls == len(judged.llm.calls)


@pytest.mark.parametrize(
    "field,value",
    [("kind", []), ("caption", 123), ("page", -1), ("page", True), ("source_pdf_sha256", 42)],
)
def test_valid_json_with_bad_document_structure_is_isolated(judged, field, value):
    snapshot = judged.ctx.store.stale_snapshot()
    damaged = snapshot.papers[0].id
    key = snapshot.fulltexts[damaged].doc_key
    payload = decompress_json(judged.ctx.blobs.get(key))
    if field == "source_pdf_sha256":
        payload[field] = value
    else:
        payload["blocks"][0][field] = value
    judged.ctx.blobs.put(key, compress_json(payload))
    result = report(judged.ctx)
    broken = [entry for entry in result.items if entry.paper_id == damaged]
    assert len(broken) == 3 and all(entry.status == "unknown" for entry in broken)
    assert all("归档" in " ".join(entry.unchecked) for entry in broken)
    assert sum(entry.status == "current" for entry in entries(result, "extraction")) == 1


def test_cli_filters_formats_and_validation(judged, monkeypatch):
    monkeypatch.setattr(cli_library, "context_or_exit", lambda _: judged.ctx)
    runner = CliRunner()
    all_entries = runner.invoke(app, ["stale", "--kind", "extraction", "--include-current"])
    assert all_entries.exit_code == 0 and "核对 2 项" in all_entries.stdout
    assert len(all_entries.stdout.splitlines()) == 3
    selected = runner.invoke(app, ["stale", "--kind", "score", "--format", "json"])
    assert selected.exit_code == 0 and json.loads(selected.stdout)["total"] == 2
    for args in (["--format", "csv"], ["--kind", "unknown"], ["--kind", "score,"]):
        response = runner.invoke(app, ["stale", *args])
        assert response.exit_code == 2 and "Traceback" not in response.output


def test_cli_empty_history_without_model_routes(harness, monkeypatch):
    ctx = replace(harness.ctx, llm=replace(harness.ctx.llm, routes={}))
    monkeypatch.setattr(cli_library, "context_or_exit", lambda _: ctx)
    response = CliRunner().invoke(app, ["stale", "--format", "json"])
    assert response.exit_code == 0, response.output
    assert json.loads(response.stdout) == {
        "total": 0,
        "current": 0,
        "stale": 0,
        "unknown": 0,
        "items": [],
    }


def test_cli_loads_real_config_without_model_file_and_preserves_history(judged):
    (judged.root / "config" / "llm.yaml").unlink()
    before = judged.ctx.store.stale_snapshot()
    response = CliRunner().invoke(
        app, ["stale", "--config", str(judged.root / "config"), "--format", "json"]
    )
    assert response.exit_code == 0, response.output
    result = json.loads(response.stdout)
    assert result["total"] == 6 and result["unknown"] == 6
    assert judged.ctx.store.stale_snapshot() == before
