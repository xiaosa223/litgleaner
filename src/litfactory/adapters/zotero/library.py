"""读本机 Zotero 库（zotero.sqlite）。

Zotero 运行时独占锁住数据库，直接打开会失败，写入更会损坏用户的库。所以每次读取都先把
zotero.sqlite 和同名 -wal 文件复制到私有临时目录，用 SQLite 只读模式打开副本，读完即删。
不复制 -shm：它属于正在运行的 Zotero 进程，SQLite 会按 -wal 重建。
复制期间 Zotero 恰好在写入时副本可能不一致，此时报 TransientError，下次重试。
"""

import shutil
import sqlite3
import tempfile
from collections.abc import Iterator, Sequence
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from litfactory.adapters.zotero.discovery import DB_NAME
from litfactory.adapters.zotero.mapping import item_arxiv, item_doi
from litfactory.domain.errors import ConfigError, SchemaChanged, TransientError

_CHUNK = 500
_REGULAR = (
    "t.typeName NOT IN ('attachment', 'note', 'annotation') "
    "AND NOT EXISTS (SELECT 1 FROM deletedItems x WHERE x.itemID = i.itemID)"
)
_ITEMS = f"""
SELECT i.itemID, i.key, i.libraryID, i.dateAdded, t.typeName
FROM items i
JOIN itemTypesCombined t ON t.itemTypeID = i.itemTypeID
JOIN libraries l ON l.libraryID = i.libraryID
WHERE l.type = 'user' AND {_REGULAR} AND i.dateAdded > ? {{collection}}
ORDER BY i.dateAdded, i.itemID
LIMIT ?
"""
_COLLECTIONS = """
SELECT c.collectionID FROM collections c JOIN libraries l ON l.libraryID = c.libraryID
WHERE l.type = 'user' AND (c.key = ? OR c.collectionName = ?)
"""
_FIELDS = """
SELECT d.itemID, f.fieldName, v.value
FROM itemData d
JOIN fieldsCombined f ON f.fieldID = d.fieldID
JOIN itemDataValues v ON v.valueID = d.valueID
WHERE d.itemID IN ({ids})
"""
_CREATORS = """
SELECT ic.itemID, t.creatorType, c.firstName, c.lastName, c.fieldMode
FROM itemCreators ic
JOIN creators c ON c.creatorID = ic.creatorID
JOIN creatorTypes t ON t.creatorTypeID = ic.creatorTypeID
WHERE ic.itemID IN ({ids})
ORDER BY ic.itemID, ic.orderIndex
"""
_BY_IDENTIFIER = f"""
SELECT DISTINCT i.itemID, i.key, i.libraryID, i.dateAdded, t.typeName
FROM items i
JOIN itemTypesCombined t ON t.itemTypeID = i.itemTypeID
JOIN itemData d ON d.itemID = i.itemID
JOIN fieldsCombined f ON f.fieldID = d.fieldID
JOIN itemDataValues v ON v.valueID = d.valueID
WHERE {_REGULAR}
  AND f.fieldName IN ('DOI', 'extra', 'url', 'archiveID')
  AND instr(lower(CAST(v.value AS TEXT)), ?) > 0
ORDER BY i.dateAdded, i.itemID
"""
_PDF_ATTACHMENTS = """
SELECT i.key, a.linkMode, a.path
FROM itemAttachments a JOIN items i ON i.itemID = a.itemID
WHERE a.parentItemID = ? AND lower(a.contentType) = 'application/pdf' AND a.linkMode IN (0, 1, 2)
  AND NOT EXISTS (SELECT 1 FROM deletedItems x WHERE x.itemID = a.itemID)
ORDER BY i.dateAdded, i.itemID
"""


@dataclass(frozen=True)
class Attachment:
    key: str
    link_mode: int
    path: str | None


@contextmanager
def snapshot(data_dir: Path) -> Iterator[sqlite3.Connection]:
    """数据库的只读临时副本。退出时关闭连接并删除副本。"""
    source = data_dir / DB_NAME
    with tempfile.TemporaryDirectory(prefix="litfactory-zotero-") as folder:
        target = Path(folder) / DB_NAME
        try:
            shutil.copyfile(source, target)
            wal = source.with_name(f"{DB_NAME}-wal")
            if wal.is_file():
                shutil.copyfile(wal, target.with_name(f"{DB_NAME}-wal"))
        except OSError as error:
            raise TransientError(f"复制 Zotero 数据库失败：{type(error).__name__}") from error
        connection = sqlite3.connect(f"{target.as_uri()}?mode=ro", uri=True)
        try:
            yield connection
        except sqlite3.OperationalError as error:
            if "no such" in str(error):
                raise SchemaChanged(f"Zotero 数据库结构与预期不符：{error}") from error
            raise TransientError(f"读取 Zotero 数据库失败：{error}") from error
        except sqlite3.DatabaseError as error:
            raise TransientError(f"Zotero 数据库副本不完整，稍后重试：{error}") from error
        finally:
            connection.close()


class LocalLibrary:
    def __init__(self, data_dir: Path) -> None:
        self.data_dir = data_dir

    def recent_items(
        self, *, after: str, limit: int, collection: str | None
    ) -> list[dict[str, Any]]:
        """用户“我的文库”里 dateAdded 晚于 after 的常规条目，按加入时间升序。"""
        with snapshot(self.data_dir) as db:
            params: list[Any] = [after]
            clause = ""
            if collection is not None:
                ids = _collection_ids(db, collection)
                clause = (
                    "AND i.itemID IN (SELECT itemID FROM collectionItems "
                    f"WHERE collectionID IN ({_marks(ids)}))"
                )
                params += ids
            rows = db.execute(_ITEMS.format(collection=clause), [*params, limit]).fetchall()
            return _load_items(db, rows)

    def pdf_attachments(self, *, doi: str | None, arxiv: str | None) -> list[Attachment]:
        """按 DOI 或 arXiv 编号找到条目（所有本地库），返回其 PDF 子附件，先加入的在前。"""
        needle = doi or arxiv
        if not needle:
            return []
        with snapshot(self.data_dir) as db:
            rows = db.execute(_BY_IDENTIFIER, (needle.lower(),)).fetchall()
            matched = [
                item
                for item in _load_items(db, rows)
                if (doi and item_doi(item) == doi) or (arxiv and item_arxiv(item) == arxiv)
            ]
            return [
                Attachment(str(key), int(mode), path if isinstance(path, str) else None)
                for item in matched
                for key, mode, path in db.execute(_PDF_ATTACHMENTS, (item["itemID"],))
            ]


def _marks(values: Sequence[Any]) -> str:
    return ",".join("?" for _ in values)


def _collection_ids(db: sqlite3.Connection, collection: str) -> list[int]:
    ids = [int(row[0]) for row in db.execute(_COLLECTIONS, (collection, collection))]
    if not ids:
        raise ConfigError(f"Zotero 我的文库里找不到 collection “{collection}”（按名称或 key 查找）")
    return ids


def _load_items(db: sqlite3.Connection, rows: Sequence[Sequence[Any]]) -> list[dict[str, Any]]:
    """把条目行补全为 Web API data 一样的结构：字段名 → 值，creators 按顺序。"""
    items = {
        int(item_id): {
            "itemID": int(item_id),
            "key": key,
            "libraryID": int(library),
            "dateAdded": added,
            "itemType": item_type,
            "creators": [],
        }
        for item_id, key, library, added, item_type in rows
    }
    for start in range(0, len(items), _CHUNK):
        chunk = list(items)[start : start + _CHUNK]
        for item_id, field, value in db.execute(_FIELDS.format(ids=_marks(chunk)), chunk):
            if value is not None:
                items[int(item_id)].setdefault(field, str(value))
        for item_id, kind, first, last, mode in db.execute(
            _CREATORS.format(ids=_marks(chunk)), chunk
        ):
            items[int(item_id)]["creators"].append(_creator(kind, first, last, mode))
    return list(items.values())


def _creator(kind: str, first: str | None, last: str | None, mode: int | None) -> dict[str, str]:
    if mode == 1:
        return {"creatorType": kind, "name": last or ""}
    return {"creatorType": kind, "firstName": first or "", "lastName": last or ""}
