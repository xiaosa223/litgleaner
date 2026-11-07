"""从本机 Zotero 数据目录读附件 PDF。

路径来自数据库，也要当作不可信输入检查：
- 导入的附件（linkMode 0/1）路径形如 "storage:文件名"，解析符号链接后必须仍在
  <data_dir>/storage/<附件 key>/ 之内，挡住 ../ 和指向外部的符号链接；
- 链接的文件（linkMode 2）必须是绝对路径、普通文件、扩展名 .pdf。
  “attachments:” 开头的相对路径依赖 Zotero 的“链接附件根目录”设置，这里读不到，跳过。
读取前检查大小上限，内容以 %PDF- 开头才返回。
"""

import logging
import stat
from pathlib import Path

from litgleaner.adapters.common.download import PDF_MAGIC
from litgleaner.adapters.zotero.library import Attachment

logger = logging.getLogger(__name__)

IMPORTED_MODES = frozenset({0, 1})
LINKED_FILE = 2
_STORAGE_PREFIX = "storage:"


def read_attachment(data_dir: Path, attachment: Attachment, max_bytes: int) -> bytes | None:
    path = _resolve(data_dir, attachment)
    if path is None:
        return None
    try:
        info = path.stat()
        if not stat.S_ISREG(info.st_mode):
            return None
        if info.st_size > max_bytes:
            logger.warning("Zotero 附件 %s 超过 %s 字节，已跳过", attachment.key, max_bytes)
            return None
        with path.open("rb") as handle:
            content = handle.read(max_bytes + 1)
    except OSError as error:
        logger.info("读取 Zotero 附件 %s 失败：%s", attachment.key, type(error).__name__)
        return None
    if len(content) > max_bytes or not content.startswith(PDF_MAGIC):
        return None
    return content


def _resolve(data_dir: Path, attachment: Attachment) -> Path | None:
    raw = attachment.path or ""
    if attachment.link_mode in IMPORTED_MODES:
        return _imported(data_dir, attachment.key, raw)
    if attachment.link_mode == LINKED_FILE:
        return _linked(raw)
    return None


def _imported(data_dir: Path, key: str, raw: str) -> Path | None:
    name = raw.removeprefix(_STORAGE_PREFIX) if raw.startswith(_STORAGE_PREFIX) else ""
    if not name or not key.isascii() or not key.isalnum():
        return None
    try:
        folder = (data_dir / "storage" / key).resolve()
        target = (folder / name).resolve()
    except (OSError, RuntimeError, ValueError):
        return None
    if target == folder or not target.is_relative_to(folder):
        logger.warning("Zotero 附件 %s 的路径越出了 storage 目录，已拒绝", key)
        return None
    return target


def _linked(raw: str) -> Path | None:
    path = Path(raw)
    if not raw or not path.is_absolute():
        return None
    try:
        target = path.resolve(strict=True)
    except (OSError, RuntimeError, ValueError):
        return None
    return target if target.suffix.lower() == ".pdf" else None
