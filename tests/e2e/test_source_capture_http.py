"""真实组装入口与模拟HTTP：捕获发生在解析前，记录实际来源环境。"""

import base64
import json
import sqlite3
from datetime import date

import httpx
import pytest

from litgleaner import bootstrap
from litgleaner.adapters.sources.crossref import CrossrefSource
from litgleaner.adapters.sources.semantic_scholar import SemanticScholarSource
from litgleaner.application.stages.discover import DiscoverStage
from litgleaner.domain.errors import AuthFailed, SchemaChanged
from litgleaner.domain.query import InboxDrain, KeywordSearch
from tests.e2e.source_capture_helpers import inputs
from tests.helpers.http import FIXTURES


def _build(harness, monkeypatch, handler, *, adapter="crossref"):
    def crossref(args):
        return CrossrefSource(
            args.name,
            args.options,
            contact_email=args.contact,
            transport=httpx.MockTransport(handler),
            today=lambda: date(2020, 2, 3),
        )

    def scholar(args):
        return SemanticScholarSource(
            args.name,
            args.options,
            contact_email=args.contact,
            api_key=args.optional_secret(bootstrap.S2_KEY),
            transport=httpx.MockTransport(handler),
            today=lambda: date(2020, 2, 3),
        )

    monkeypatch.setitem(bootstrap.SOURCES, adapter, crossref if adapter == "crossref" else scholar)

    def settings(config):
        config["sources"] = {
            "alpha": {
                "adapter": adapter,
                "optional_env": [bootstrap.S2_KEY] if adapter == "semantic_scholar" else [],
                "options": {"base_url": "https://recording.invalid", "min_interval_seconds": 0},
            }
        }

    harness.edit_yaml("sources.yaml", settings)
    return harness.rebuild()


def _run(ctx):
    DiscoverStage().process(
        ctx, {"source": "alpha", "query": KeywordSearch(terms=("alpha",)).model_dump()}
    )


@pytest.mark.parametrize("mode", ["invalid_json", "auth_failed", "returned"])
def test_http_is_in_sqlite_and_blob_before_json_or_status_checks(harness, monkeypatch, mode):
    status = 401 if mode == "auth_failed" else 200
    body = b"broken JSON" if mode == "invalid_json" else b'{"message":{"items":[]}}'
    ctx = _build(harness, monkeypatch, lambda _: httpx.Response(status, content=body))
    original = httpx.Response.json
    parsed = []

    def inspect_before_parse(response, **kwargs):
        (attempt,) = ctx.store.source_captures.list("alpha")
        assert attempt.status == "started" and attempt.finished_at is None
        pages = [json.loads(raw) for kind, raw in inputs(ctx, attempt) if kind == "http"]
        assert len(pages) == 1 and base64.b64decode(pages[0]["body"]) == body
        assert ctx.store.stats()["paper"] == 0
        parsed.append(True)
        return original(response, **kwargs)

    monkeypatch.setattr(httpx.Response, "json", inspect_before_parse)
    if mode == "returned":
        _run(ctx)
    else:
        with pytest.raises(AuthFailed if mode == "auth_failed" else SchemaChanged):
            _run(ctx)
    (attempt,) = ctx.store.source_captures.list("alpha")
    assert parsed == ([] if mode == "auth_failed" else [True])
    assert attempt.status == ("returned" if mode == "returned" else "raised")
    captured = inputs(ctx, attempt)
    assert [kind for kind, _ in captured] == ["today", "http"]
    assert json.loads(captured[0][1]) == "2020-02-03"
    assert attempt.started_at.startswith("2026-11-03")
    assert attempt.recipe.options["timeout_seconds"] == 30
    assert attempt.recipe.option_fields == ("base_url", "min_interval_seconds")
    assert attempt.recipe.credentials == {} and harness.llm_calls() == 0


def test_bootstrap_auth_presence_and_contact_are_frozen_without_persisting_key(
    harness, monkeypatch
):
    secret = 'synthetic-key+"/\\'
    monkeypatch.setenv(bootstrap.S2_KEY, secret)
    monkeypatch.setenv(bootstrap.CONTACT_EMAIL, "original@example.invalid")
    body = json.dumps({"data": [], "echo": secret}).encode()
    ctx = _build(
        harness,
        monkeypatch,
        lambda _: httpx.Response(200, content=body),
        adapter="semantic_scholar",
    )
    monkeypatch.setenv(bootstrap.CONTACT_EMAIL, "changed@example.invalid")
    _run(ctx)
    (attempt,) = ctx.store.source_captures.list("alpha")
    assert attempt.recipe.credentials == {bootstrap.S2_KEY: True}
    assert attempt.recipe.contact == "original@example.invalid"
    page = next(json.loads(raw) for kind, raw in inputs(ctx, attempt) if kind == "http")
    assert json.loads(base64.b64decode(page["body"]))["echo"] == "[secret]"
    assert "x-api-key" not in page["headers"] and secret not in repr(attempt)
    assert all(secret.encode() not in raw for _, raw in inputs(ctx, attempt))


def test_redirects_are_each_durable_and_blob_failure_stops_before_following(harness, monkeypatch):
    calls = []

    def handler(request):
        calls.append(str(request.url))
        return httpx.Response(302, headers={"Location": "https://second.invalid/works"})

    ctx = _build(harness, monkeypatch, handler)
    original = ctx.blobs.put

    def fail_page(key, content):
        if key.startswith("source_inputs/") and "/000001-" in key:
            raise OSError("archive unavailable")
        return original(key, content)

    monkeypatch.setattr(ctx.blobs, "put", fail_page)
    with pytest.raises(OSError):
        _run(ctx)
    (attempt,) = ctx.store.source_captures.list("alpha")
    assert len(calls) == 1 and attempt.status == "raised" and attempt.error_type == "OSError"
    assert [kind for kind, _ in inputs(ctx, attempt)] == ["today"]
    assert ctx.store.papers.get_cursor("alpha", attempt.query_hash) is None
    assert ctx.store.stats()["paper"] == 0


def test_local_library_capture_keeps_raw_boundary_rows_in_real_receipt(harness, monkeypatch):
    directory = harness.root / "input-library"
    directory.mkdir()
    with sqlite3.connect(directory / "zotero.sqlite") as db:
        db.executescript((FIXTURES / "zotero/schema.sql").read_text())
        for item_id, added in ((1, "08:00:00"), (2, "09:00:00"), (3, "09:00:00")):
            db.execute(
                "INSERT INTO items(itemID,itemTypeID,dateAdded,libraryID,key) VALUES(?,22,?,1,?)",
                (item_id, f"2026-11-01 {added}", f"ITEM000{item_id}"),
            )
            db.execute(
                "INSERT INTO itemDataValues(valueID,value) VALUES(?,?)", (item_id, f"P{item_id}")
            )
            db.execute("INSERT INTO itemData VALUES(?,1,?)", (item_id, item_id))
    monkeypatch.delenv(bootstrap.ZOTERO_KEY, raising=False)

    def settings(config):
        config["sources"] = {
            "local": {
                "adapter": "zotero",
                "optional_env": [bootstrap.ZOTERO_KEY],
                "options": {"data_dir": str(directory), "max_records": 2, "lookback_days": None},
            }
        }

    harness.edit_yaml("sources.yaml", settings)
    ctx = harness.rebuild()
    DiscoverStage().process(ctx, {"source": "local", "query": InboxDrain().model_dump()})
    (attempt,) = ctx.store.source_captures.list("local")
    sql = [json.loads(raw) for kind, raw in inputs(ctx, attempt) if kind == "sql"]
    assert len(sql[0]["rows"]) == 3 and sql[0]["parameters"] == ["", 3]
    assert sql[0]["rows"][-1][0] == 3
    assert attempt.recipe.options["max_records"] == 2 and attempt.status == "returned"
    assert attempt.result["records"] == ctx.store.stats()["paper"] == 1
    assert attempt.recipe.credentials == {bootstrap.ZOTERO_KEY: False}
    assert harness.llm_calls() == 0
