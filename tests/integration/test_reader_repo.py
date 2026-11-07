"""阅读状态和批注的存取。"""

from pathlib import Path

import pytest

from litgleaner.domain.paper import Paper
from litgleaner.domain.reading import NewAnnotation
from litgleaner.infra.db.migrate import upgrade_database
from litgleaner.infra.db.store import SqlStore


@pytest.fixture
def store(tmp_path: Path) -> SqlStore:
    url = f"sqlite:///{tmp_path / 'reader.db'}"
    upgrade_database(url)
    store = SqlStore(url)
    store.papers.insert(Paper("P1", "T", "t", None, ("A",), None, None, None, "2026-01-01"))
    return store


def test_reading_state_upserts_and_keeps_position(store: SqlStore):
    assert store.reader.get_state("P1") is None
    store.reader.set_state("P1", "queued", None, "t1")
    store.reader.set_state("P1", "reading", {"page": 3}, "t2")

    state = store.reader.get_state("P1")
    assert state is not None
    assert (state.status, state.position, state.updated_at) == ("reading", {"page": 3}, "t2")
    assert store.reader.papers_with_status("reading") == ("P1",)
    assert store.reader.papers_with_status("queued") == ()


def test_reading_state_rejects_unknown_paper(store: SqlStore):
    with pytest.raises(Exception):  # noqa: B017 外键约束由数据库给出
        store.reader.set_state("missing", "queued", None, "t1")


def test_annotations_are_listed_in_order_and_soft_deleted(store: SqlStore):
    first = store.reader.add_annotation(
        NewAnnotation(
            "P1",
            "highlight",
            page=1,
            bbox=(1, 2, 3, 4),
            quote="We propose",
            body=None,
            rects=((10, 20, 300, 12), (10, 34, 120, 12)),
        ),
        "t1",
    )
    second = store.reader.add_annotation(
        NewAnnotation("P1", "note", page=2, bbox=None, quote=None, body="要复现"), "t2"
    )

    assert [item.id for item in store.reader.annotations("P1")] == [first.id, second.id]
    assert store.reader.annotations("P1")[0].bbox == (1, 2, 3, 4)
    assert store.reader.annotations("P1")[0].rects == ((10, 20, 300, 12), (10, 34, 120, 12))
    assert store.reader.annotations("P1")[1].rects == ()

    assert store.reader.update_annotation(second.id, "要先复现基线", "t3")
    assert store.reader.annotations("P1")[1].body == "要先复现基线"

    assert store.reader.delete_annotation(first.id, "t4")
    assert [item.id for item in store.reader.annotations("P1")] == [second.id]
    assert not store.reader.delete_annotation(first.id, "t5")
    assert not store.reader.update_annotation("missing", "x", "t6")
