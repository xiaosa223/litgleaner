"""导出目录与 ZIP 的文件边界；只写新目录，不覆盖已有数据。"""

import re
import tempfile
from collections.abc import Iterable
from contextlib import ExitStack, suppress
from pathlib import Path
from typing import BinaryIO, cast
from zipfile import ZIP_DEFLATED, ZipFile, ZipInfo

from litgleaner.domain.errors import InvalidRequest
from litgleaner.domain.export import ExportFile


class _Names:
    def __init__(self) -> None:
        self.files: set[str] = set()
        self.directories: set[str] = set()

    def add(self, name: str) -> tuple[str, ...]:
        parts = tuple(name.split("/"))
        reserved = re.compile(r"(?:CON|PRN|AUX|NUL|COM[1-9]|LPT[1-9])(?:\.|$)", re.I)
        if any(
            part in ("", ".", "..")
            or not re.fullmatch(r"[A-Za-z0-9_.-]+", part)
            or part.endswith(".")
            or reserved.match(part)
            for part in parts
        ):
            raise InvalidRequest("导出包包含非法文件名")
        key = name.casefold()
        parents = {"/".join(parts[:index]).casefold() for index in range(1, len(parts))}
        if key in self.files or key in self.directories or parents & self.files:
            raise InvalidRequest("导出包包含重复或冲突的文件名")
        self.files.add(key)
        self.directories.update(parents)
        return parts


def write_directory(entries: Iterable[ExportFile], out: Path) -> int:
    names = _Names()
    files: list[Path] = []
    directories: list[Path] = []
    complete = False
    try:
        out.parent.mkdir(parents=True, exist_ok=True)
        try:
            out.mkdir(mode=0o700)
        except FileExistsError as error:
            raise InvalidRequest("导出目录已存在，请选择一个新目录") from error
        directories.append(out)
        for entry in entries:
            parts = names.add(entry.name)
            parent = out
            for part in parts[:-1]:
                parent = parent / part
                if parent not in directories:
                    parent.mkdir(mode=0o700)
                    directories.append(parent)
            target = out.joinpath(*parts)
            with target.open("xb") as stream:
                files.append(target)
                stream.write(entry.content)
        complete = True
    except OSError as error:
        raise InvalidRequest("导出写入失败，请检查目录权限与可用空间") from error
    finally:
        if not complete:
            _cleanup(files, directories)
    return len(names.files)


def _cleanup(files: list[Path], directories: list[Path]) -> None:
    for path in reversed(files):
        with suppress(OSError):
            path.unlink(missing_ok=True)
    for path in reversed(directories):
        with suppress(OSError):
            path.rmdir()
            # 并发出现的其他文件不属于本次导出，保留非空目录。


def zip_archive(entries: Iterable[ExportFile]) -> BinaryIO:
    """失败自动释放临时文件；成功将关闭责任交给响应或其他调用方。"""
    names = _Names()
    with ExitStack() as resources:
        archive = resources.enter_context(
            tempfile.SpooledTemporaryFile(max_size=8 * 1024 * 1024, mode="w+b")
        )
        with ZipFile(archive, "w", compression=ZIP_DEFLATED, allowZip64=True) as zipped:
            for entry in entries:
                names.add(entry.name)
                info = ZipInfo(entry.name, date_time=(1980, 1, 1, 0, 0, 0))
                info.compress_type = ZIP_DEFLATED
                info.external_attr = 0o100600 << 16
                zipped.writestr(info, entry.content)
        archive.seek(0)
        resources.pop_all()
        return cast(BinaryIO, archive)
