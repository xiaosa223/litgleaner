"""Zotero 本地库：信号源（InboxDrain）和全文来源。

测试库在 tmp_path 里按真实 zotero.sqlite 表结构子集（tests/fixtures/zotero/schema.sql）现建，
条目内容全部虚构，不使用任何用户的真实文献库。
"""

import hashlib
import json
import sqlite3
from datetime import date
from pathlib import Path
from typing import Any

import pytest
from tests.helpers.http import FIXTURES

from litfactory.adapters.zotero.fulltext import ZoteroFullText
from litfactory.adapters.zotero.source import ZoteroSource
from litfactory.domain.errors import ConfigError, SchemaChanged
from litfactory.domain.identity import Alias
from litfactory.domain.query import InboxDrain, KeywordSearch
from litfactory.ports.fulltext import FullTextRequest

TODAY = date(2026, 11, 5)
PDF = b"%PDF-1.7\nzotero body"
TYPES = {"annotation": 1, "attachment": 3, "book": 7, "conferencePaper": 11}
TYPES |= {"journalArticle": 22, "note": 28, "preprint": 31}
FIELDS = {"title": 1, "abstractNote": 2, "date": 6, "DOI": 8, "url": 10, "extra": 19}
FIELDS |= {"publicationTitle": 41, "ISSN": 44, "proceedingsTitle": 65, "archiveID": 107}
CREATOR_TYPES = {"author": 10, "editor": 12}


class FakeZotero:
    """按真实表结构写一个最小 Zotero 库。creators 元素：(类型, 名, 姓) 或 (类型, 单字段姓名)。"""

    def __init__(self, root: Path) -> None:
        self.root = root
        self.db = root / "zotero.sqlite"
        self.conn = sqlite3.connect(self.db)
        self.conn.executescript((FIXTURES / "zotero" / "schema.sql").read_text(encoding="utf-8"))
        self._next = 1

    def item(
        self,
        key: str,
        item_type: str = "journalArticle",
        *,
        added: str = "2026-11-01 08:00:00",
        library: int = 1,
        creators: tuple[tuple[str, ...], ...] = (),
        **fields: str,
    ) -> int:
        item_id = self._next
        self._next += 1
        self.conn.execute(
            "INSERT INTO items (itemID, itemTypeID, dateAdded, libraryID, key) VALUES (?,?,?,?,?)",
            (item_id, TYPES[item_type], added, library, key),
        )
        for name, value in fields.items():
            self._field(item_id, name, value)
        for index, creator in enumerate(creators):
            self._creator(item_id, index, creator)
        self.conn.commit()
        return item_id

    def _field(self, item_id: int, name: str, value: str) -> None:
        self.conn.execute("INSERT OR IGNORE INTO itemDataValues (value) VALUES (?)", (value,))
        (value_id,) = self.conn.execute(
            "SELECT valueID FROM itemDataValues WHERE value = ?", (value,)
        ).fetchone()
        self.conn.execute("INSERT INTO itemData VALUES (?,?,?)", (item_id, FIELDS[name], value_id))

    def _creator(self, item_id: int, index: int, creator: tuple[str, ...]) -> None:
        kind, *names = creator
        first, last, mode = (names[0], names[1], 0) if len(names) == 2 else ("", names[0], 1)
        cursor = self.conn.execute(
            "INSERT INTO creators (firstName, lastName, fieldMode) VALUES (?,?,?)",
            (first, last, mode),
        )
        self.conn.execute(
            "INSERT INTO itemCreators VALUES (?,?,?,?)",
            (item_id, cursor.lastrowid, CREATOR_TYPES[kind], index),
        )

    def attach(
        self,
        parent: int | None,
        key: str,
        *,
        link_mode: int = 0,
        path: str | None = "storage:paper.pdf",
        content_type: str = "application/pdf",
        content: bytes | None = PDF,
    ) -> int:
        item_id = self.item(key, "attachment")
        self.conn.execute(
            "INSERT INTO itemAttachments (itemID, parentItemID, linkMode, contentType, path) "
            "VALUES (?,?,?,?,?)",
            (item_id, parent, link_mode, content_type, path),
        )
        self.conn.commit()
        if content is not None and path and path.startswith("storage:"):
            folder = self.root / "storage" / key
            folder.mkdir(parents=True, exist_ok=True)
            (folder / path.removeprefix("storage:")).write_bytes(content)
        return item_id

    def trash(self, item_id: int) -> None:
        self.conn.execute("INSERT INTO deletedItems (itemID) VALUES (?)", (item_id,))
        self.conn.commit()

    def collection(self, name: str, key: str, item_ids: tuple[int, ...]) -> None:
        cursor = self.conn.execute(
            "INSERT INTO collections (collectionName, libraryID, key) VALUES (?, 1, ?)", (name, key)
        )
        for item_id in item_ids:
            self.conn.execute(
                "INSERT INTO collectionItems VALUES (?,?,0)", (cursor.lastrowid, item_id)
            )
        self.conn.commit()


@pytest.fixture
def zot(tmp_path: Path) -> FakeZotero:
    root = tmp_path / "Zotero"
    root.mkdir()
    return FakeZotero(root)


def source(zot: FakeZotero, **options: Any) -> ZoteroSource:
    return ZoteroSource(
        "zotero",
        {"data_dir": str(zot.root), **options},
        contact_email=None,
        today=lambda: TODAY,
    )


def titles(result: Any) -> list[str]:
    return [record.title for record in result.records]


# ---- 信号源 ----


def test_declares_inbox_drain_only(zot: FakeZotero):
    adapter = source(zot)
    assert adapter.capabilities == frozenset({InboxDrain})
    with pytest.raises(ValueError, match="不支持"):
        adapter.fetch(KeywordSearch(terms=("x",)), None)


def test_first_run_reads_recent_regular_items_only(zot: FakeZotero):
    zot.item("OLD00001", added="2026-09-01 10:00:00", title="Too old")
    paper = zot.item("NEW00001", added="2026-10-20 10:00:00", title="Recent paper")
    zot.item("NEW00002", "preprint", added="2026-11-02 09:30:00", title="Recent preprint")
    zot.item("NOTE0001", "note", added="2026-11-02 10:00:00")
    zot.item("ANNO0001", "annotation", added="2026-11-02 10:00:00")
    zot.attach(paper, "ATTACH01")
    zot.trash(zot.item("TRASH001", added="2026-11-03 10:00:00", title="In the trash"))
    zot.item("GROUP001", added="2026-11-03 10:00:00", library=2, title="Group item")

    result = source(zot).fetch(InboxDrain(), None)

    assert titles(result) == ["Recent paper", "Recent preprint"]
    assert result.next_cursor == "2026-11-02 09:30:00"
    assert result.warnings == ()


def test_record_fields_are_mapped(zot: FakeZotero):
    zot.item(
        "ABCD2345",
        added="2026-11-01 08:00:00",
        creators=(
            ("author", "Alice", "Smith"),
            ("editor", "Ed", "Itor"),
            ("author", "Research Consortium"),
            ("author", "Bo", "Li"),
        ),
        title="Graph learning for proteins",
        abstractNote="<p>We study <i>graphs</i>.</p>",
        publicationTitle="Journal of Examples",
        ISSN="1234-5678, 8765-4321",
        date="2024-03-00 March 2024",
        DOI="https://doi.org/10.1000/ABC.123",
        extra="PMID: 123456\nPMCID: PMC777",
    )
    (record,) = source(zot).fetch(InboxDrain(), None).records
    assert record.title == "Graph learning for proteins"
    assert record.abstract == "We study graphs ."
    assert record.authors == ("Alice Smith", "Research Consortium", "Bo Li")
    assert (record.venue, record.issn) == ("Journal of Examples", "1234-5678")
    assert record.published_at == date(2024, 3, 1)
    assert record.doi == "10.1000/abc.123"
    assert record.external_ids == {"zotero": "1/ABCD2345", "pmid": "123456", "pmcid": "PMC777"}


def test_identifiers_from_extra_url_and_proceedings_venue(zot: FakeZotero):
    zot.item(
        "CONF0001",
        "conferencePaper",
        title="Conference paper with DOI in extra",
        proceedingsTitle="Proceedings of Examples",
        date="2023-00-00 2023",
        extra="Citation Key: x\nDOI: 10.5555/Conf.1",
    )
    zot.item(
        "ARXIV001",
        "preprint",
        title="Preprint linked to arXiv",
        url="https://arxiv.org/abs/2401.01234v2",
    )
    zot.item(
        "ARXIV002", "preprint", title="Preprint with arXiv in extra", extra="arXiv: 2402.05678"
    )
    records = {r.title: r for r in source(zot).fetch(InboxDrain(), None).records}
    conference = records["Conference paper with DOI in extra"]
    assert (conference.doi, conference.venue) == ("10.5555/conf.1", "Proceedings of Examples")
    assert conference.published_at == date(2023, 1, 1)
    assert records["Preprint linked to arXiv"].arxiv_id == "2401.01234"
    assert records["Preprint with arXiv in extra"].arxiv_id == "2402.05678"


def test_cursor_returns_only_items_added_after_it(zot: FakeZotero):
    zot.item("SEEN0001", added="2026-11-01 08:00:00", title="Already seen")
    zot.item("NEXT0001", added="2026-11-01 08:00:01", title="Added later")
    result = source(zot).fetch(InboxDrain(), "2026-11-01 08:00:00")
    assert titles(result) == ["Added later"]
    assert result.next_cursor == "2026-11-01 08:00:01"
    again = source(zot).fetch(InboxDrain(), result.next_cursor)
    assert again.records == ()
    assert again.next_cursor == "2026-11-01 08:00:01"


def test_first_run_without_lookback_imports_whole_history(zot: FakeZotero):
    zot.item("ANCIENT1", added="2001-01-01 00:00:00", title="Very old paper")
    assert titles(source(zot, lookback_days=None).fetch(InboxDrain(), None)) == ["Very old paper"]
    empty = source(zot, lookback_days=30).fetch(InboxDrain(), None)
    assert empty.records == ()
    assert empty.next_cursor == "2026-10-06 00:00:00"


def test_max_records_truncates_at_timestamp_boundary(zot: FakeZotero):
    zot.item("ITEM0001", added="2026-11-01 08:00:00", title="First item")
    zot.item("ITEM0002", added="2026-11-01 09:00:00", title="Second item")
    zot.item("ITEM0003", added="2026-11-01 09:00:00", title="Third item, same second")
    result = source(zot, max_records=2).fetch(InboxDrain(), None)
    assert titles(result) == ["First item"]
    assert result.next_cursor == "2026-11-01 08:00:00"
    assert any("上限" in warning for warning in result.warnings)
    rest = source(zot, max_records=2).fetch(InboxDrain(), result.next_cursor)
    assert titles(rest) == ["Second item", "Third item, same second"]


def test_collection_by_name_or_key(zot: FakeZotero):
    inside = zot.item("INSIDE01", title="Inside the inbox collection")
    zot.item("OUTSIDE1", title="Somewhere else")
    zot.collection("To Read", "COLL0001", (inside,))
    for value in ("To Read", "COLL0001"):
        assert titles(source(zot, collection=value).fetch(InboxDrain(), None)) == [
            "Inside the inbox collection"
        ]
    with pytest.raises(ConfigError, match="collection"):
        source(zot, collection="Missing").fetch(InboxDrain(), None)


def test_raw_is_json_of_items_read(zot: FakeZotero):
    zot.item("RAWITEM1", title="Raw item", DOI="10.1/raw")
    raw = source(zot).fetch(InboxDrain(), None).raw
    assert raw is not None
    (item,) = json.loads(raw)
    assert (item["key"], item["title"], item["DOI"]) == ("RAWITEM1", "Raw item", "10.1/raw")


def digest(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest() if path.exists() else "-"


def test_reads_locked_wal_database_without_touching_it(zot: FakeZotero):
    zot.conn.close()
    live = sqlite3.connect(zot.db)
    live.execute("PRAGMA journal_mode=WAL")
    live.execute("PRAGMA locking_mode=EXCLUSIVE")
    live.execute(
        "INSERT INTO items (itemID, itemTypeID, dateAdded, libraryID, key) "
        "VALUES (99, 22, '2026-11-04 12:00:00', 1, 'WALITEM1')"
    )
    live.execute("INSERT INTO itemDataValues (valueID, value) VALUES (99, 'Only in the WAL')")
    live.execute("INSERT INTO itemData VALUES (99, 1, 99)")
    live.commit()
    wal = zot.db.with_name("zotero.sqlite-wal")
    before = (digest(zot.db), digest(wal))
    assert wal.stat().st_size > 0
    try:
        result = source(zot).fetch(InboxDrain(), None)
    finally:
        after = (digest(zot.db), digest(wal))
        live.close()
    assert titles(result) == ["Only in the WAL"]
    assert before == after


def test_unexpected_database_structure_is_schema_changed(zot: FakeZotero):
    zot.conn.execute("DROP TABLE deletedItems")
    zot.conn.commit()
    with pytest.raises(SchemaChanged):
        source(zot).fetch(InboxDrain(), None)


@pytest.mark.parametrize(
    "options",
    [
        {"mode": "cloud"},
        {"lookback_days": -1},
        {"unknown": 1},
        {"mode": "web"},
        {"mode": "web", "user_id": 1, "group_id": 2},
    ],
)
def test_invalid_options_are_config_errors(zot: FakeZotero, options: dict[str, Any]):
    with pytest.raises(ConfigError):
        source(zot, **options)


def test_data_dir_without_database_is_config_error(tmp_path: Path):
    with pytest.raises(ConfigError, match="data_dir"):
        ZoteroSource("zotero", {"data_dir": str(tmp_path)}, contact_email=None)


# ---- 全文 ----


def fulltext(zot: FakeZotero, **options: Any) -> ZoteroFullText:
    return ZoteroFullText({"data_dir": str(zot.root), **options}, contact_email=None)


def request(*aliases: Alias) -> FullTextRequest:
    return FullTextRequest("P1", "Some paper", aliases)


def test_fulltext_reads_imported_pdf_by_doi(zot: FakeZotero):
    paper = zot.item("PAPER001", title="Paper", DOI="10.1000/Example.42")
    zot.attach(paper, "SNAPSHOT", path="storage:page.html", content_type="text/html")
    zot.attach(paper, "PDFFILE1", path="storage:Paper - 2024.pdf")
    found = fulltext(zot).fetch(request(Alias("doi", "10.1000/example.42")))
    assert found is not None
    assert (found.content, found.license_basis, found.provider) == (PDF, "user_supplied", "zotero")
    assert ZoteroFullText.name == "zotero"


def test_fulltext_matches_doi_in_extra_and_arxiv_in_url(zot: FakeZotero):
    first = zot.item("PAPER002", title="Paper", extra="DOI: 10.2000/EXTRA")
    zot.attach(first, "PDFFILE2")
    second = zot.item(
        "PAPER003", "preprint", title="Preprint", url="http://arxiv.org/abs/2401.01234"
    )
    zot.attach(second, "PDFFILE3")
    assert fulltext(zot).fetch(request(Alias("doi", "10.2000/extra"))) is not None
    assert fulltext(zot).fetch(request(Alias("arxiv", "2401.01234"))) is not None


def test_fulltext_reads_linked_pdf(zot: FakeZotero, tmp_path: Path):
    linked = tmp_path / "elsewhere" / "linked.pdf"
    linked.parent.mkdir()
    linked.write_bytes(PDF)
    paper = zot.item("PAPER004", title="Paper", DOI="10.3000/linked")
    zot.attach(paper, "LINKED01", link_mode=2, path=str(linked))
    found = fulltext(zot).fetch(request(Alias("doi", "10.3000/linked")))
    assert found is not None and found.content == PDF


def test_fulltext_rejects_unsafe_paths(zot: FakeZotero, tmp_path: Path):
    secret = tmp_path / "secret.pdf"
    secret.write_bytes(PDF)
    text_file = tmp_path / "notes.txt"
    text_file.write_bytes(PDF)
    paper = zot.item("PAPER005", title="Paper", DOI="10.4000/unsafe")
    zot.attach(paper, "ESCAPE01", path="storage:../../secret.pdf", content=None)
    zot.attach(paper, "LINKTXT1", link_mode=2, path=str(text_file))
    zot.attach(paper, "RELATIVE", link_mode=2, path="attachments:paper.pdf")
    link_dir = zot.root / "storage" / "SYMLINK1"
    link_dir.mkdir(parents=True)
    (link_dir / "paper.pdf").symlink_to(secret)
    zot.attach(paper, "SYMLINK1", content=None)
    assert fulltext(zot).fetch(request(Alias("doi", "10.4000/unsafe"))) is None


def test_fulltext_skips_non_pdf_and_oversized_files(zot: FakeZotero):
    paper = zot.item("PAPER006", title="Paper", DOI="10.5000/checks")
    zot.attach(paper, "NOTAPDF1", content=b"<html>login</html>")
    assert fulltext(zot).fetch(request(Alias("doi", "10.5000/checks"))) is None
    zot.attach(paper, "BIGFILE1", content=PDF + b"x" * 1_100_000)
    assert fulltext(zot, max_pdf_mb=1).fetch(request(Alias("doi", "10.5000/checks"))) is None
    found = fulltext(zot, max_pdf_mb=2).fetch(request(Alias("doi", "10.5000/checks")))
    assert found is not None and found.content.startswith(PDF)


def test_fulltext_returns_none_without_match(zot: FakeZotero):
    trashed = zot.item("PAPER007", title="Paper", DOI="10.6000/trashed")
    zot.attach(trashed, "PDFFILE7")
    zot.trash(trashed)
    adapter = fulltext(zot)
    assert adapter.fetch(request(Alias("doi", "10.6000/trashed"))) is None
    assert adapter.fetch(request(Alias("doi", "10.9999/unknown"))) is None
    assert adapter.fetch(request(Alias("pmid", "123"))) is None
    assert adapter.fetch(request()) is None


def test_fulltext_paths_with_nul_bytes_are_ignored(zot: FakeZotero):
    paper = zot.item("PAPER009", title="Paper", DOI="10.5000/nul")
    zot.attach(paper, "NULPATH1", path="storage:a\x00.pdf", content=None)
    zot.attach(paper, "NULLINK1", link_mode=2, path="/tmp/a\x00.pdf", content=None)
    assert fulltext(zot).fetch(request(Alias("doi", "10.5000/nul"))) is None
