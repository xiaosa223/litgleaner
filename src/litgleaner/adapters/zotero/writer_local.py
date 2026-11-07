"""把选中的论文写进本机 Zotero：建条目、放进分组、挂推荐理由笔记和 PDF。

请求细节（授权、Server-ID、错误分类）在 local_api.py。这里负责：
- 查重：本地 API 的快速搜索不查 DOI，按标题搜，再核对 DOI / arXiv 编号。
- 已有的条目只补分组和标签，不重复建；笔记和 PDF 先看是否已经挂过。
- 文件上传三步：申请（表单 md5/filename/filesize/mtime 毫秒，If-None-Match: *）→
  把文件发到返回的 url（params=1 时用 multipart 的 file 字段）→ 用 upload=<uploadKey> 登记。
"""

import hashlib
import logging
import re
import time
from collections.abc import Callable, Mapping
from typing import Any

import httpx

from litgleaner.adapters.zotero.local_api import (
    AUTHORIZE_HINT,
    DEFAULT_BASE_URL,
    USER_PREFIX,
    LocalApi,
    ServerChanged,
    parse_json,
)
from litgleaner.adapters.zotero.mapping import is_regular, item_arxiv, item_doi
from litgleaner.domain.errors import AuthFailed, SchemaChanged, TransientError
from litgleaner.domain.identity import normalize_arxiv_id, normalize_doi, title_key
from litgleaner.domain.library import LibraryEntry, note_marker
from litgleaner.ports.library import LibraryReceipt

logger = logging.getLogger(__name__)

DEFAULT_COLLECTION = "拾穗"
DEFAULT_TAGS = ("拾穗",)
_CJK = re.compile(r"[　-鿿가-힯]")


def creator(name: str) -> dict[str, str]:
    """西文名拆成名和姓；中日韩姓名、单个词保持整体（Zotero 的单字段模式）。"""
    cleaned = " ".join(name.split())
    if "," in cleaned:
        last, _, first = cleaned.partition(",")
        if last.strip() and first.strip():
            return {"creatorType": "author", "lastName": last.strip(), "firstName": first.strip()}
    if _CJK.search(cleaned) or " " not in cleaned:
        return {"creatorType": "author", "name": cleaned}
    first, _, last = cleaned.rpartition(" ")
    return {"creatorType": "author", "firstName": first, "lastName": last}


def item_json(entry: LibraryEntry, collection_key: str, tags: tuple[str, ...]) -> dict[str, Any]:
    """预印本（有 arXiv 编号、没有期刊）用 preprint，其余都当期刊论文。"""
    preprint = bool(entry.arxiv_id) and not entry.venue
    item: dict[str, Any] = {
        "itemType": "preprint" if preprint else "journalArticle",
        "title": entry.title,
        "creators": [creator(name) for name in entry.authors if name.strip()],
        "abstractNote": entry.abstract or "",
        "date": entry.published_at or "",
        "DOI": entry.doi or "",
        "url": entry.url or "",
        "tags": [{"tag": tag} for tag in tags],
        "collections": [collection_key],
    }
    if preprint:
        item["repository"] = "arXiv"
        item["archiveID"] = f"arXiv:{entry.arxiv_id}"
    else:
        item["publicationTitle"] = entry.venue or ""
        if entry.arxiv_id:
            item["extra"] = f"arXiv: {entry.arxiv_id}"
    return item


def write_token(*parts: str) -> str:
    """5～32 个字符。同一篇、同一步重试时相同，Zotero 没重启时能挡住重复写入。"""
    return hashlib.sha256(":".join(parts).encode("utf-8")).hexdigest()[:32]


def same_paper(entry: LibraryEntry, data: Mapping[str, Any]) -> bool:
    doi, arxiv = normalize_doi(entry.doi), normalize_arxiv_id(entry.arxiv_id)
    if doi or arxiv:
        return bool((doi and item_doi(data) == doi) or (arxiv and item_arxiv(data) == arxiv))
    # 没有任何标识符时才按标题判断，避免同名不同文的论文被当成一篇。
    title = data.get("title")
    return isinstance(title, str) and title_key(title) == title_key(entry.title)


class ZoteroLocalLibrary:
    name = "zotero"

    def __init__(
        self,
        options: Mapping[str, Any],
        *,
        api_key: str | None,
        transport: httpx.BaseTransport | None = None,
        clock: Callable[[], float] = time.time,
    ) -> None:
        self._collection_name = str(options.get("collection") or DEFAULT_COLLECTION)
        self._tags = tuple(str(tag) for tag in options.get("tags", DEFAULT_TAGS))
        self._attach_pdf = bool(options.get("attach_pdf", True))
        self._clock = clock
        self._collection_key: str | None = None
        self._api = LocalApi(
            str(options.get("base_url") or DEFAULT_BASE_URL),
            api_key=api_key,
            timeout=float(options.get("timeout", 30)),
            transport=transport,
        )

    @property
    def collection_name(self) -> str:
        return self._collection_name

    def close(self) -> None:
        self._api.close()

    def probe(self) -> str:
        """检查 Zotero 在运行、本地 API 已开启，返回 Server-ID。不写入任何东西。"""
        return self._api.server_id(refresh=True)

    def authorize(self) -> str:
        return self._api.authorize()

    def save(self, entry: LibraryEntry) -> LibraryReceipt:
        if not self._api.has_key:
            raise AuthFailed(f"没有 Zotero 本地 key：{AUTHORIZE_HINT}")
        try:
            return self._save(entry)
        except ServerChanged:
            self._collection_key = None
            raise

    def _save(self, entry: LibraryEntry) -> LibraryReceipt:
        tags = tuple(dict.fromkeys((*self._tags, *entry.tags)))
        collection = self.collection_key()
        existing = self.find_existing(entry)
        children: list[Mapping[str, Any]] = []
        if existing is not None:
            key = str(existing["key"])
            self._merge_into(existing, collection, tags)
            children = self._api.objects(f"{USER_PREFIX}/items/{key}/children")
        else:
            key = self._create(entry, collection, tags)
        if not any(_is_note_for(child, entry.key) for child in children):
            # 返回 None 说明 Zotero 没重启、上一次已经写过，正好不用再写。
            note = {"itemType": "note", "parentItem": key, "note": entry.note_html, "tags": []}
            self._api.post_one("/items", note, write_token("note", entry.key))
        warnings: tuple[str, ...] = ()
        if not any(_is_pdf(child) for child in children):
            warnings = self._attach(key, entry)
        return LibraryReceipt(item_key=key, created=existing is None, warnings=warnings)

    def collection_key(self) -> str:
        if self._collection_key is None:
            for entry in self._api.objects(f"{USER_PREFIX}/collections"):
                if entry["data"].get("name") == self._collection_name:
                    self._collection_key = str(entry["key"])
                    break
            else:
                token = write_token("collection", self._collection_name)
                body = {"name": self._collection_name}
                self._collection_key = _required(self._api.post_one("/collections", body, token))
        return self._collection_key

    def find_existing(self, entry: LibraryEntry) -> Mapping[str, Any] | None:
        params = {"q": entry.title, "qmode": "titleCreatorYear", "format": "json"}
        for item in self._api.objects(f"{USER_PREFIX}/items", params):
            data = item["data"]
            if is_regular(data) and same_paper(entry, data):
                return item
        return None

    def _create(self, entry: LibraryEntry, collection: str, tags: tuple[str, ...]) -> str:
        body = item_json(entry, collection, tags)
        return _required(self._api.post_one("/items", body, write_token("item", entry.key)))

    def _merge_into(
        self, existing: Mapping[str, Any], collection: str, tags: tuple[str, ...]
    ) -> None:
        data = existing["data"]
        collections = list(data.get("collections") or [])
        current = [t.get("tag") for t in data.get("tags") or [] if isinstance(t, Mapping)]
        missing = [tag for tag in tags if tag not in current]
        if collection in collections and not missing:
            return
        patch = {
            "collections": list(dict.fromkeys([*collections, collection])),
            "tags": [*(data.get("tags") or []), *({"tag": tag} for tag in missing)],
        }
        version = str(existing.get("version", data.get("version", 0)))
        self._api.send(
            "PATCH",
            f"{USER_PREFIX}/items/{existing['key']}",
            json=patch,
            headers={"If-Unmodified-Since-Version": version},
        )

    def _attach(self, parent: str, entry: LibraryEntry) -> tuple[str, ...]:
        """PDF 放不进去不影响条目本身：删掉没有文件的空附件，只返回警告。"""
        if not self._attach_pdf or entry.pdf is None:
            return ()
        token = write_token("pdf", entry.key)
        written = self._api.post_one("/items", _attachment(parent, entry), token)
        if written is None:  # 上一次尝试已经建过附件
            return ()
        key, version = written
        try:
            self._upload_pdf(key, entry)
        except (TransientError, SchemaChanged) as error:
            logger.warning("Zotero 附件 %s 上传失败：%s", key, error)
            self._discard(key, version)
            return (f"PDF 没有放进 Zotero：{error}",)
        return ()

    def _discard(self, key: str, version: int) -> None:
        try:
            headers = {"If-Unmodified-Since-Version": str(version)}
            self._api.send("DELETE", f"{USER_PREFIX}/items/{key}", headers=headers)
        except (TransientError, SchemaChanged, AuthFailed) as error:
            logger.warning("删除空附件 %s 失败，请在 Zotero 里手动删除：%s", key, error)

    def _upload_pdf(self, key: str, entry: LibraryEntry) -> None:
        pdf = entry.pdf or b""
        filename = entry.pdf_filename
        path = f"{USER_PREFIX}/items/{key}/file"
        form = {
            "md5": hashlib.md5(pdf, usedforsecurity=False).hexdigest(),
            "filename": filename,
            "filesize": str(len(pdf)),
            "mtime": str(int(self._clock() * 1000)),
            "contentType": "application/pdf",
            "params": "1",
        }
        precondition = {"If-None-Match": "*"}
        grant = parse_json(self._api.send("POST", path, data=form, headers=precondition))
        if isinstance(grant, Mapping) and grant.get("exists"):
            return
        if not isinstance(grant, Mapping) or not grant.get("uploadKey") or not grant.get("url"):
            raise SchemaChanged("Zotero 上传授权响应缺少 url 或 uploadKey")
        upload_path = httpx.URL(str(grant["url"])).raw_path.decode("ascii")
        if not upload_path.startswith("/api/local/uploads/"):
            raise SchemaChanged(f"Zotero 返回了意外的上传地址：{upload_path[:80]}")
        files = {"file": (filename, pdf, "application/pdf")}
        self._api.send("POST", upload_path, files=files, auth=False, server_id=False)
        upload = {"upload": str(grant["uploadKey"])}
        self._api.send("POST", path, data=upload, headers=precondition)


def _required(written: tuple[str, int] | None) -> str:
    """条目和分组不能“已经写过”就算完：上次结果未知，交给下次重试，那时查重会找到它。"""
    if written is None:
        raise TransientError("Zotero 说上次已经写入，但还查不到，稍后重试")
    return written[0]


def _attachment(parent: str, entry: LibraryEntry) -> dict[str, Any]:
    return {
        "itemType": "attachment",
        "parentItem": parent,
        "linkMode": "imported_file",
        "title": "Full Text PDF",
        "contentType": "application/pdf",
        "filename": entry.pdf_filename,
        "tags": [],
    }


def _is_pdf(child: Mapping[str, Any]) -> bool:
    data = child["data"]
    return bool(
        data.get("itemType") == "attachment" and data.get("contentType") == "application/pdf"
    )


def _is_note_for(child: Mapping[str, Any], paper_id: str) -> bool:
    data = child["data"]
    note = data.get("note")
    is_note = data.get("itemType") == "note" and isinstance(note, str)
    return is_note and note_marker(paper_id) in str(note)
