"""非 HTTP 输入、真实来源日期与生效选项的捕获合同。"""

import base64
import json
import sqlite3
from datetime import date

import httpx
import pytest
from tests.helpers.http import FIXTURES

from litgleaner.adapters.common.capture import (
    CapturedSqlReader,
    capture_inputs,
    captured_today,
    configured_options,
)
from litgleaner.adapters.fakes.source import FakeSource
from litgleaner.adapters.sources.arxiv import ArxivSource
from litgleaner.adapters.sources.core import CoreSource
from litgleaner.adapters.sources.crossref import CrossrefSource
from litgleaner.adapters.sources.europe_pmc import EuropePmcSource
from litgleaner.adapters.sources.openalex import OpenAlexSource
from litgleaner.adapters.sources.openreview import OpenReviewSource
from litgleaner.adapters.sources.pubmed import PubMedSource
from litgleaner.adapters.sources.semantic_scholar import SemanticScholarSource
from litgleaner.adapters.zotero.source import ZoteroSource
from litgleaner.domain.errors import LitGleanerError
from litgleaner.domain.query import InboxDrain, KeywordSearch

TODAY = date(2026, 11, 5)


class Sink:
    def __init__(self):
        self.events = []

    def append(self, kind, content):
        self.events.append((kind, content))


def test_fake_raw_is_saved_before_invalid_json(tmp_path):
    path = tmp_path / "records.json"
    path.write_bytes(b'{"broken":')
    source = FakeSource("files", path)
    sink = Sink()
    with capture_inputs(sink), pytest.raises(json.JSONDecodeError):
        source.fetch(KeywordSearch(terms=("alpha",)), None)
    assert sink.events == [("file", b'{"broken":')]


def test_failed_file_capture_prevents_json_parse(tmp_path, monkeypatch):
    class FailedSink:
        def append(self, kind, content):
            raise OSError("no space")

    parsed = []
    monkeypatch.setattr(json, "loads", parsed.append)
    path = tmp_path / "records.json"
    path.write_bytes(b"[]")
    with capture_inputs(FailedSink()), pytest.raises(OSError, match="no space"):
        FakeSource("files", path).fetch(KeywordSearch(terms=("alpha",)), None)
    assert parsed == []


def test_each_actual_clock_call_is_recorded_in_order():
    values = iter((TODAY, date(2026, 11, 6)))
    today = captured_today(lambda: next(values))
    sink = Sink()
    with capture_inputs(sink):
        assert today() == TODAY
        assert today() == date(2026, 11, 6)
    assert [json.loads(raw) for kind, raw in sink.events if kind == "today"] == [
        "2026-11-05",
        "2026-11-06",
    ]


@pytest.mark.parametrize(
    ("factory", "options", "credentials"),
    [
        (ArxivSource, {}, {}),
        (CoreSource, {}, {"api_key": "offline-capture-key"}),
        (CrossrefSource, {}, {}),
        (EuropePmcSource, {}, {}),
        (OpenAlexSource, {}, {}),
        (OpenReviewSource, {}, {}),
        (PubMedSource, {}, {}),
        (SemanticScholarSource, {}, {}),
        (ZoteroSource, {"mode": "web", "user_id": 1}, {"api_key": "offline-capture-key"}),
    ],
)
def test_every_http_source_captures_injected_date_before_response(factory, options, credentials):
    source = factory(
        "recorded",
        {"min_interval_seconds": 3, **options},
        contact_email=None,
        today=lambda: TODAY,
        transport=httpx.MockTransport(lambda _: httpx.Response(200, content=b"not JSON or XML")),
        **credentials,
    )
    query = InboxDrain() if factory is ZoteroSource else KeywordSearch(terms=("alpha",))
    sink = Sink()
    with capture_inputs(sink), pytest.raises(LitGleanerError):
        source.fetch(query, None)
    kinds = [kind for kind, _ in sink.events]
    assert kinds[0] == "today"
    assert "http" in kinds
    assert json.loads(sink.events[0][1]) == TODAY.isoformat()


def test_effective_options_include_defaults_and_explicit_fields_remain_separate():
    source = SemanticScholarSource("recorded", {"max_records": 2}, contact_email=None)
    effective, explicit = configured_options(source)
    assert effective["max_records"] == 2
    assert effective["min_interval_seconds"] == 3
    assert explicit == ("max_records",)
    assert source._http._min_interval == 3
    assert configured_options(object()) is None
    source._http.close()


def _local_database(tmp_path):
    db = sqlite3.connect(tmp_path / "zotero.sqlite")
    db.executescript((FIXTURES / "zotero" / "schema.sql").read_text())
    for item_id, added in ((1, "08:00:00"), (2, "09:00:00"), (3, "09:00:00")):
        db.execute(
            "INSERT INTO items(itemID,itemTypeID,dateAdded,libraryID,key) VALUES(?,22,?,1,?)",
            (item_id, f"2026-11-01 {added}", f"ITEM000{item_id}"),
        )
        db.execute(
            "INSERT INTO itemDataValues(valueID,value) VALUES(?,?)", (item_id, f"P{item_id}")
        )
        db.execute("INSERT INTO itemData VALUES(?,1,?)", (item_id, item_id))
    db.commit()
    db.close()


def test_local_sql_capture_keeps_unadopted_boundary_rows(tmp_path):
    _local_database(tmp_path)
    source = ZoteroSource(
        "local",
        {"data_dir": str(tmp_path), "max_records": 2},
        contact_email=None,
        today=lambda: TODAY,
    )
    sink = Sink()
    with capture_inputs(sink):
        result = source.fetch(InboxDrain(), None)
    assert [record.title for record in result.records] == ["P1"]
    events = [json.loads(raw) for kind, raw in sink.events if kind == "sql"]
    assert len(events) == 3
    assert [row[0] for row in events[0]["rows"]] == [1, 2, 3]
    assert events[0]["parameters"] == ["2026-10-06 00:00:00", 3]
    assert "itemData" in events[1]["sql"]
    assert "itemCreators" in events[2]["sql"]


def test_local_raw_sql_survives_failure_before_item_mapping(tmp_path, monkeypatch):
    _local_database(tmp_path)
    sink = Sink()

    def failed_mapping(db, rows):
        assert [row[0] for row in rows] == [1, 2, 3]
        assert any(kind == "sql" for kind, _ in sink.events)
        raise ValueError("fixed by future decoder")

    monkeypatch.setattr("litgleaner.adapters.zotero.library._load_items", failed_mapping)
    source = ZoteroSource("local", {"data_dir": str(tmp_path)}, contact_email=None)
    with capture_inputs(sink), pytest.raises(ValueError, match="future decoder"):
        source.fetch(InboxDrain(), None)
    event = next(json.loads(raw) for kind, raw in sink.events if kind == "sql")
    assert len(event["rows"]) == 3


def test_sql_capture_preserves_binary_bindings_and_values():
    db = sqlite3.connect(":memory:")
    reader = CapturedSqlReader(db)
    sink = Sink()
    with capture_inputs(sink):
        assert list(reader.execute("SELECT ?, ?", (b"\xff\x00", None))) == [(b"\xff\x00", None)]
    ((kind, raw),) = sink.events
    event = json.loads(raw)
    assert kind == "sql"
    assert event["parameters"] == event["rows"][0]
    assert base64.b64decode(event["parameters"][0]["base64"]) == b"\xff\x00"
    db.close()


def test_failed_sql_capture_stops_before_item_mapping(tmp_path, monkeypatch):
    class FailedSink:
        def append(self, kind, content):
            if kind == "sql":
                raise OSError("archive failed")

    mapped = []
    monkeypatch.setattr("litgleaner.adapters.zotero.library._load_items", mapped.append)
    _local_database(tmp_path)
    source = ZoteroSource("local", {"data_dir": str(tmp_path)}, contact_email=None)
    with capture_inputs(FailedSink()), pytest.raises(OSError, match="archive failed"):
        source.fetch(InboxDrain(), None)
    assert mapped == []
