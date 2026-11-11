"""课题对比表和引用真实贯通数据库、零费用下载与 CLI 输出。"""

import csv
import io
import json
import socket
from dataclasses import replace

import pytest
from fastapi.testclient import TestClient
from typer.testing import CliRunner

from litgleaner import cli_library
from litgleaner.application import topic_export
from litgleaner.application.stages.discover import resolve_record
from litgleaner.application.topics import pause_topic
from litgleaner.cli import app as cli_app
from litgleaner.domain.errors import InvalidRequest, NotFound
from litgleaner.domain.identity import Alias
from litgleaner.domain.paper import SourceRecord
from litgleaner.ports.store import TriageResult
from litgleaner.server.app import create_app
from litgleaner.server.security import LocalSecurity
from tests.e2e.conftest import Harness, extraction_answer, paper

TOPIC = "alpha-topic"
BASE = f"/api/topics/{TOPIC}"


@pytest.fixture
def exported(harness: Harness):
    quote = "We study the problem."
    harness.llm.overrides["extract"] = extraction_answer(
        method={"text": "可复用的方法", "quote": quote, "page": 0},
        datasets=[],
        metrics=[],
        results=[{"text": "未核验结果", "quote": "An invented numerical result.", "page": 0}],
        code_data=[],
    )
    harness.write_papers(
        [paper("High", 5), paper("Low", 3), paper("Irrelevant", 5, marker="[[irrelevant]]")]
    )
    assert harness.run().ok
    ids = {
        name: next(
            iter(
                harness.ctx.store.papers.find_ids_by_aliases(
                    (Alias("doi", f"10.1000/{name.lower()}"),)
                )
            )
        )
        for name in ("High", "Low", "Irrelevant")
    }
    missing = resolve_record(
        harness.ctx.store.papers,
        SourceRecord(title="Unidentified relevant paper"),
        harness.ctx.now_iso(),
    )
    topic = harness.ctx.topics[TOPIC]
    harness.ctx.store.judgments.save_triage(
        TriageResult(missing, TOPIC, topic.topic_hash, True, "local", "v", "model"),
        harness.ctx.now_iso(),
    )
    ids["Missing"] = missing
    harness.ctx.store.reader.set_state(ids["High"], "read", {"page": 0}, harness.ctx.now_iso())
    return harness, ids


def test_scope_covers_pushed_and_unscored_relevant_papers_but_excludes_rejected(exported):
    harness, ids = exported
    before = harness.llm_calls()
    result = topic_export.comparison(harness.ctx, TOPIC)
    assert result.total == 3 and {row.paper_id for row in result.rows} == {
        ids["High"],
        ids["Low"],
        ids["Missing"],
    }
    assert [row.paper_id for row in result.rows] == sorted(row.paper_id for row in result.rows)
    rows = {row.paper_id: row for row in result.rows}
    assert rows[ids["High"]].reading_value == 100 and not rows[ids["High"]].extraction_stale
    assert rows[ids["Missing"]].reading_value is None
    table = topic_export.table_text(result, "md")
    assert "可复用的方法" in table and "未核验结果" in table and "未核验" in table
    assert "本次提取未获得可核验证据" in table and "未提取" in table
    csv_rows = list(csv.reader(io.StringIO(topic_export.table_text(result, "csv"))))
    assert len(csv_rows) == 4 and all(len(row) == 9 for row in csv_rows)
    selected = topic_export.comparison(harness.ctx, TOPIC, tier="must_read,must_read")
    assert selected.total == 3 and selected.tiers == ("must_read",)
    assert [row.paper_id for row in selected.rows] == [ids["High"]]
    assert harness.llm_calls() == before


def test_refs_only_use_source_metadata_and_separate_unidentified_records(exported):
    harness, ids = exported
    result = topic_export.references(harness.ctx, TOPIC)
    assert result.total == 3
    assert [paper.id for paper in result.needs_verification] == [ids["Missing"]]
    records = json.loads(topic_export.reference_text(result, "csl-json"))
    assert {item["id"] for item in records} == {ids["High"], ids["Low"]}
    assert all("可复用的方法" not in json.dumps(item, ensure_ascii=False) for item in records)
    selected = topic_export.references(harness.ctx, TOPIC, only_read=True)
    assert selected.total == 1 and selected.only_read
    assert json.loads(selected.references.csl_json)[0]["id"] == ids["High"]


@pytest.mark.parametrize("change", ["language", "budget", "policy", "semantic"])
def test_invalidated_scores_never_pass_tier_filter_or_become_zero(exported, change):
    harness, _ids = exported
    ctx = harness.ctx
    if change in ("language", "budget"):
        settings = (
            {"reading_language": "en"} if change == "language" else {"assess_token_budget": 2000}
        )
        ctx = replace(ctx, settings=replace(ctx.settings, **settings))
    else:
        rubric = ctx.rubrics["default"]
        dimensions = tuple(
            dimension.model_copy(
                update={"weight": dimension.weight + (1 if index == 0 else -1 if index == 1 else 0)}
                if change == "policy"
                else {"question": dimension.question + " changed"}
            )
            for index, dimension in enumerate(rubric.dimensions)
        )
        ctx = replace(
            ctx, rubrics={"default": rubric.model_copy(update={"dimensions": dimensions})}
        )
    before = harness.llm_calls()
    result = topic_export.comparison(ctx, TOPIC)
    assert result.total == 3 and all(row.reading_value is None for row in result.rows)
    assert not topic_export.comparison(ctx, TOPIC, tier="must_read,digest").rows
    assert harness.llm_calls() == before


def test_legacy_extraction_remains_missing_and_stale_without_backfill(exported):
    harness, ids = exported
    harness.ctx.store.papers.insert_extraction(
        ids["High"], "legacy-input", "legacy-version", {"summary": "Older saved summary"}, "t"
    )
    before = harness.llm_calls()
    rows = topic_export.comparison(harness.ctx, TOPIC).rows
    row = next(row for row in rows if row.paper_id == ids["High"])
    assert row.extraction_stale and row.extraction == {"summary": "Older saved summary"}
    table = topic_export.table_text(topic_export.comparison(harness.ctx, TOPIC), "md")
    assert "提取已过期" in table and "未提取" in table
    assert harness.llm_calls() == before


def test_changed_topic_scope_returns_empty_and_unknown_topic_or_tier_is_rejected(exported):
    harness, _ids = exported
    topic = harness.ctx.topics[TOPIC]
    changed = replace(
        harness.ctx, topics={TOPIC: topic.model_copy(update={"background": "Changed"})}
    )
    assert topic_export.comparison(changed, TOPIC).rows == ()
    assert topic_export.references(changed, TOPIC).total == 0
    with pytest.raises(NotFound):
        topic_export.comparison(harness.ctx, "missing")
    for value in ("", "unknown", "must_read,", ",digest"):
        with pytest.raises(InvalidRequest):
            topic_export.comparison(harness.ctx, TOPIC, tier=value)


def test_paused_offline_exports_do_not_mutate_or_read_blobs_or_network(exported, monkeypatch):
    harness, _ids = exported
    pause_topic(harness.ctx, TOPIC)
    harness.ctx = replace(harness.ctx, llm=replace(harness.ctx.llm, routes={}, daily_budget_usd=0))
    before, calls = harness.ctx.store.stats(), harness.llm_calls()

    def forbidden(*_args, **_kwargs):
        pytest.fail("read-only export performed external or mutation work")

    monkeypatch.setattr(harness.ctx.blobs, "get", forbidden)
    monkeypatch.setattr(harness.ctx.store._db, "write", forbidden)
    monkeypatch.setattr(socket, "create_connection", forbidden)
    assert len(topic_export.comparison(harness.ctx, TOPIC).rows) == 3
    assert topic_export.references(harness.ctx, TOPIC).total == 3
    assert harness.ctx.store.stats() == before and harness.llm_calls() == calls


@pytest.mark.parametrize("format", ["bad", "gbt7714"])
def test_unknown_or_unconfigured_reference_formats_are_rejected(exported, format):
    harness, _ids = exported
    with pytest.raises(InvalidRequest):
        topic_export.reference_text(topic_export.references(harness.ctx, TOPIC), format)
    with pytest.raises(InvalidRequest):
        topic_export.table_text(topic_export.comparison(harness.ctx, TOPIC), format)


@pytest.fixture
def export_ui(exported, monkeypatch):
    harness, ids = exported
    monkeypatch.setattr(cli_library, "context_or_exit", lambda _: harness.ctx)
    security = LocalSecurity(48147, launch_token="topic-export")
    app = create_app(harness.ctx, security, None, context_provider=lambda: harness.ctx)
    with TestClient(app, base_url="http://127.0.0.1:48147") as client:
        yield harness, ids, client


@pytest.mark.parametrize(
    "path", ["comparison-table", "references", "exports/table", "exports/refs"]
)
def test_api_requires_a_local_session_and_allows_get_without_csrf(export_ui, path):
    harness, _ids, client = export_ui
    before = harness.llm_calls()
    assert client.get(BASE + "/" + path).status_code == 401
    client.get("/launch", params={"token": "topic-export"})
    response = client.get(BASE + "/" + path)
    assert response.status_code == 200, response.text
    assert harness.llm_calls() == before


def test_download_formats_missing_list_and_headers_match_saved_scope(export_ui):
    _harness, ids, client = export_ui
    client.get("/launch", params={"token": "topic-export"})
    for format, content_type in (("md", "text/markdown"), ("csv", "text/csv")):
        response = client.get(BASE + "/exports/table", params={"format": format})
        assert response.status_code == 200 and content_type in response.headers["content-type"]
        assert "attachment" in response.headers["content-disposition"]
        assert response.headers["cache-control"] == "no-store"
        assert response.headers["x-content-type-options"] == "nosniff"
    response = client.get(BASE + "/exports/refs", params={"format": "csl-json"})
    assert response.status_code == 200 and len(response.json()) == 2
    assert response.headers["x-litgleaner-needs-verification"] == "1"
    view = client.get(BASE + "/references").json()
    assert [paper["id"] for paper in view["needs_verification"]] == [ids["Missing"]]
    assert client.get(BASE + "/references", params={"only_read": True}).json()["total"] == 1
    assert client.get(BASE + "/exports/refs", params={"format": "gbt7714"}).status_code == 422
    assert client.get(BASE + "/exports/table", params={"tier": "unknown"}).status_code == 422
    assert client.get("/api/topics/missing/references").status_code == 404


def test_cli_stdout_is_machine_readable_and_manual_verification_uses_stderr(export_ui):
    _harness, ids, _client = export_ui
    runner = CliRunner()
    response = runner.invoke(cli_app, ["export", "refs", "--topic", TOPIC, "--format", "csl-json"])
    assert response.exit_code == 0, response.output
    assert len(json.loads(response.stdout)) == 2
    assert "需要人工核对" in response.stderr and ids["Missing"] in response.stderr
    result = runner.invoke(cli_app, ["export", "table", "--topic", TOPIC, "--format", "csv"])
    assert result.exit_code == 0, result.output
    assert len(list(csv.reader(io.StringIO(result.stdout)))) == 4
    read = runner.invoke(
        cli_app, ["export", "refs", "--topic", TOPIC, "--only-read", "--format", "csl-json"]
    )
    assert read.exit_code == 0 and json.loads(read.stdout)[0]["id"] == ids["High"]


@pytest.mark.parametrize(
    "arguments",
    [
        ["refs", "--format", "gbt7714"],
        ["table", "--format", "json"],
        ["table", "--tier", "unknown"],
        ["refs", "--topic", "missing"],
    ],
)
def test_cli_rejects_invalid_inputs_without_traceback(export_ui, arguments):
    _harness, _ids, _client = export_ui
    topic_args = [] if "--topic" in arguments else ["--topic", TOPIC]
    result = CliRunner().invoke(cli_app, ["export", *arguments, *topic_args])
    assert result.exit_code == 2 and "Traceback" not in result.output
