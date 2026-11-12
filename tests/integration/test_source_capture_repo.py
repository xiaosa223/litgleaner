"""来源采集回执耐久保存，输入连续追加且终态不可重写。"""

from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from threading import Barrier

import pytest
from sqlalchemy import delete, event, insert, inspect, select, update
from sqlalchemy.exc import IntegrityError

from litgleaner.infra.db import source_capture_repo
from litgleaner.infra.db.migrate import upgrade_database
from litgleaner.infra.db.schema import source_capture_attempt, source_capture_input
from litgleaner.infra.db.store import SqlStore
from litgleaner.ports.source_capture import (
    CaptureAttempt,
    CapturedInput,
    CaptureScope,
    SourceRecipe,
)


@pytest.fixture
def store(tmp_path):
    url = f"sqlite:///{tmp_path / 'capture.db'}"
    upgrade_database(url)
    result = SqlStore(url)
    yield result
    result.dispose()


def attempt(id_="01CAPTURE", **changes):
    return replace(
        CaptureAttempt(
            id_,
            "alpha",
            {"kind": "keyword_search", "terms": ["雪", "Alpha"], "exclude": ["旧"]},
            "query-hash",
            "",
            CaptureScope("survey", {"round": 2, "owner": {"topic": "研究", "ids": ["B", "A"]}}),
            SourceRecipe(
                "recorded_source",
                {"query": ["第二", "第一"], "limits": {"end": 30, "start": 0}},
                ("limits", "query"),
                {"TOKEN": True, "OPTIONAL_TOKEN": False},
                "reader@example.test",
            ),
            "2026-11-12T08:00:00+08:00",
        ),
        **changes,
    )


def item(id_="01CAPTURE", position=0, **changes):
    return replace(
        CapturedInput(id_, position, "http", f"raw/{position}.zst", "a" * 64, 12),
        **changes,
    )


def test_started_roundtrip_preserves_query_order_unicode_recipe_and_empty_cursor(store):
    original = attempt()
    store.source_captures.start(original)
    loaded = store.source_captures.get(original.id)
    assert loaded == original
    assert list(loaded.query) == ["kind", "terms", "exclude"]
    assert list(loaded.recipe.options["limits"]) == ["end", "start"]
    assert loaded.cursor == "" and loaded.scope.owner["owner"]["ids"] == ["B", "A"]
    store.source_captures.start(attempt("other", cursor=None, recipe=None))
    assert store.source_captures.get("other").cursor is None
    assert store.source_captures.get("missing") is None


def test_contiguous_inputs_and_returned_boundary_are_immutable(store):
    store.source_captures.start(attempt())
    store.source_captures.append(item())
    store.source_captures.append(item(position=1, kind="clock", size=0))
    assert store.source_captures.inputs("01CAPTURE") == (
        item(),
        item(position=1, kind="clock", size=0),
    )
    store.source_captures.finish(
        "01CAPTURE", "returned", "2026-11-12T00:01:00Z", result={"cursor": None}
    )
    loaded = store.source_captures.get("01CAPTURE")
    assert loaded.status == "returned" and loaded.result == {"cursor": None}
    with pytest.raises(ValueError):
        store.source_captures.append(item(position=2))
    with pytest.raises(ValueError):
        store.source_captures.finish(
            "01CAPTURE", "raised", "2026-11-12T00:02:00Z", error_type="Failure"
        )


@pytest.mark.parametrize("position", [1, -1, True, 0.5])
def test_input_requires_started_receipt_and_zero_based_continuity(store, position):
    with pytest.raises(ValueError):
        store.source_captures.append(item())
    store.source_captures.start(attempt())
    with pytest.raises(ValueError):
        store.source_captures.append(item(position=position))
    store.source_captures.append(item())
    with pytest.raises(ValueError):
        store.source_captures.append(item())
    assert store.source_captures.inputs("missing") == ()


def test_start_id_cannot_replace_an_existing_attempt(store):
    original = attempt()
    store.source_captures.start(original)
    with pytest.raises(ValueError):
        store.source_captures.start(replace(original, source="other", query={"changed": True}))
    assert store.source_captures.get(original.id) == original


@pytest.mark.parametrize(
    "changes",
    [
        {"status": "returned"},
        {"finished_at": "2026-11-12T00:01:00Z"},
        {"result": {}},
        {"error_type": "TimeoutError"},
        {"query": []},
        {"cursor": True},
        {"started_at": "not-a-time"},
        {"scope": CaptureScope("wrong", {})},
        {"recipe": SourceRecipe("source", {}, (), {"SECRET": "private-value"}, None)},
    ],
)
def test_invalid_start_leaves_no_receipt(store, changes):
    with pytest.raises(ValueError):
        store.source_captures.start(attempt(**changes))
    assert store.source_captures.list("alpha") == ()


@pytest.mark.parametrize(
    "status,kwargs",
    [
        ("started", {}),
        ("returned", {}),
        ("returned", {"result": {}, "error_type": "TimeoutError"}),
        ("returned", {"result": []}),
        ("raised", {}),
        ("raised", {"error_type": "TimeoutError", "result": {}}),
        ("raised", {"error_type": "TimeoutError: secret token"}),
        ("raised", {"error_type": ""}),
    ],
)
def test_finish_requires_exactly_one_terminal_boundary(store, status, kwargs):
    store.source_captures.start(attempt())
    with pytest.raises(ValueError):
        store.source_captures.finish("01CAPTURE", status, "2026-11-12T00:01:00Z", **kwargs)
    assert store.source_captures.get("01CAPTURE").status == "started"


def test_started_failed_and_returned_attempts_are_stably_pageable_by_real_time(store):
    starts = {
        "C": "2026-11-12T00:00:00.000001Z",
        "A": "2026-11-11T23:59:59.999999Z",
        "B": "2026-11-12T01:00:00+01:00",
    }
    for id_, started_at in starts.items():
        store.source_captures.start(attempt(id_, started_at=started_at))
    store.source_captures.start(attempt("D", source="other"))
    store.source_captures.finish(
        "B", "raised", "2026-11-12T00:01:00Z", error_type="errors.TimeoutError"
    )
    store.source_captures.finish("C", "returned", "2026-11-12T00:01:00Z", result={})
    first = store.source_captures.list("alpha", limit=2)
    assert [row.id for row in first] == ["A", "B"]
    assert [row.id for row in store.source_captures.list("alpha", after=first[-1].id)] == ["C"]
    expected = ["B", "C"]
    assert [row.id for row in store.source_captures.list("alpha", "2026-11-12")] == expected
    assert [
        row.id for row in store.source_captures.list("alpha", "2026-11-11T16:00:00-08:00")
    ] == expected
    assert store.source_captures.get("B").result is None
    assert store.source_captures.get("B").error_type == "errors.TimeoutError"


@pytest.mark.parametrize("limit", [0, -1, 1001, True, "10", 1.5])
def test_list_validates_page_limit(store, limit):
    with pytest.raises(ValueError):
        store.source_captures.list("alpha", limit=limit)


def test_unknown_finish_and_bad_time_do_not_create_or_end_receipts(store):
    with pytest.raises(ValueError):
        store.source_captures.finish("missing", "returned", "2026-11-12", result={})
    store.source_captures.start(attempt())
    with pytest.raises(ValueError):
        store.source_captures.finish("01CAPTURE", "returned", "not-a-time", result={})
    with pytest.raises(ValueError):
        store.source_captures.list("alpha", "not-a-time")
    assert store.source_captures.get("01CAPTURE").status == "started"


def test_list_has_no_hidden_fifty_window_and_pages_without_skips(store):
    with store.atomic():
        for index in reversed(range(65)):
            store.source_captures.start(attempt(f"C{index:03}"))
    assert len(store.source_captures.list("alpha")) == 65
    seen, cursor = [], None
    while page := store.source_captures.list("alpha", limit=13, after=cursor):
        seen.extend(row.id for row in page)
        cursor = page[-1].id
    assert seen == [f"C{index:03}" for index in range(65)]


@pytest.mark.parametrize(
    "changes",
    [
        {"size": -1},
        {"size": True},
        {"size": 2**63},
        {"sha256": "bad"},
        {"kind": ""},
        {"blob_key": " "},
    ],
)
def test_invalid_input_metadata_never_appends(store, changes):
    store.source_captures.start(attempt())
    with pytest.raises(ValueError):
        store.source_captures.append(item(**changes))
    assert store.source_captures.inputs("01CAPTURE") == ()


def test_outer_business_transaction_can_roll_back_capture_methods(store):
    with pytest.raises(RuntimeError, match="rollback"), store.atomic():
        store.source_captures.start(attempt())
        store.source_captures.append(item())
        store.source_captures.finish("01CAPTURE", "returned", "2026-11-12T00:01:00Z", result={})
        raise RuntimeError("rollback")
    assert store.source_captures.get("01CAPTURE") is None
    assert store.source_captures.inputs("01CAPTURE") == ()
    assert store._db._active is None


def test_sql_guards_prevent_replacement_reordering_and_terminal_updates(store):
    store.source_captures.start(attempt())
    store.source_captures.append(item())
    raw = dict(store._db.fetch(select(source_capture_attempt))[0]._mapping)
    statements = [
        insert(source_capture_attempt).prefix_with("OR REPLACE").values(**raw),
        update(source_capture_attempt).values(source="changed"),
        update(source_capture_input).values(blob_key="changed"),
        delete(source_capture_input),
        insert(source_capture_input).values(
            attempt_id="01CAPTURE", position=2, kind="http", blob_key="x", sha256="b" * 64, size=1
        ),
    ]
    for statement in statements:
        with pytest.raises(IntegrityError):
            store._db.write(statement)
    store.source_captures.finish(
        "01CAPTURE", "raised", "2026-11-12T00:01:00Z", error_type="TimeoutError"
    )
    with pytest.raises(IntegrityError):
        store._db.write(update(source_capture_attempt).values(error_type="OtherError"))
    assert store.source_captures.inputs("01CAPTURE") == (item(),)


def test_foreign_key_cascade_removes_input_indexes_only_with_parent(store):
    store.source_captures.start(attempt())
    store.source_captures.append(item())
    foreign_key = inspect(store._db._engine).get_foreign_keys("source_capture_input")[0]
    assert foreign_key["referred_table"] == "source_capture_attempt"
    assert foreign_key["options"]["ondelete"] == "CASCADE"
    store._db.write(delete(source_capture_attempt))
    assert store.source_captures.inputs("01CAPTURE") == ()


def test_sql_cannot_begin_terminal_or_append_after_termination(store):
    store.source_captures.start(attempt())
    store.source_captures.append(item())
    raw = dict(store._db.fetch(select(source_capture_attempt))[0]._mapping)
    raw.update(id="already-returned", status="returned", finished_at="2026-11-12", result="{}")
    with pytest.raises(IntegrityError):
        store._db.write(insert(source_capture_attempt).values(**raw))
    fields = dict(store._db.fetch(select(source_capture_input))[0]._mapping)
    fields["position"] = 1
    store.source_captures.finish("01CAPTURE", "returned", "2026-11-12T00:01:00Z", result={})
    with pytest.raises(IntegrityError):
        store._db.write(insert(source_capture_input).values(**fields))


def test_single_queries_convert_after_release_and_do_not_load_inputs_for_each_attempt(
    store, monkeypatch
):
    store.source_captures.start(attempt())
    store.source_captures.append(item())
    convert = source_capture_repo._attempt
    statements = []

    def observed(row):
        assert store._db._active is None
        return convert(row)

    def capture(_conn, _cursor, sql, _params, _context, _many):
        statements.append(sql)

    monkeypatch.setattr(source_capture_repo, "_attempt", observed)
    event.listen(store._db._engine, "before_cursor_execute", capture)
    try:
        assert store.source_captures.get("01CAPTURE") is not None
        assert len(store.source_captures.list("alpha")) == 1
        assert len(store.source_captures.inputs("01CAPTURE")) == 1
    finally:
        event.remove(store._db._engine, "before_cursor_execute", capture)
    assert len(statements) == 3 and all(sql.startswith("SELECT") for sql in statements)


@pytest.mark.parametrize("operation", ["append", "finish"])
def test_concurrent_calls_cannot_duplicate_positions_or_terminal_states(store, operation):
    store.source_captures.start(attempt())
    other = SqlStore(str(store._db._engine.url))
    gate = Barrier(2)

    def execute(target):
        gate.wait(timeout=5)
        try:
            if operation == "append":
                target.source_captures.append(item())
            else:
                target.source_captures.finish(
                    "01CAPTURE", "returned", "2026-11-12T00:01:00Z", result={}
                )
            return True
        except ValueError:
            return False

    try:
        with ThreadPoolExecutor(max_workers=2) as executor:
            results = list(executor.map(execute, (store, other)))
        assert sorted(results) == [False, True]
        if operation == "append":
            assert store.source_captures.inputs("01CAPTURE") == (item(),)
        else:
            assert store.source_captures.get("01CAPTURE").status == "returned"
    finally:
        other.dispose()
