"""本地模型资产清单：只读状态核对文件状态，计算前后核对内容。"""

import hashlib
import json
import os
import tempfile
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

from litgleaner.domain.hashing import stable_hash

MANIFEST_VERSION = "local-embedding-assets-v1"
_MAX_MANIFEST = 1024 * 1024
_MAX_FILES = 4096


@dataclass(frozen=True)
class Asset:
    path: str
    size: int
    mtime_ns: int
    sha256: str


@dataclass(frozen=True)
class Assets:
    model: str
    dimensions: int
    directory: str
    files: tuple[Asset, ...]

    @property
    def version(self) -> str:
        return stable_hash([(item.path, item.sha256) for item in self.files])


def resolved(path: Path) -> Path:
    try:
        return path.resolve()
    except RuntimeError as error:
        raise ValueError("invalid_model_asset_symlink") from error


def within(root: Path, relative: str) -> Path:
    path = Path(relative)
    if path.is_absolute() or ".." in path.parts or not relative:
        raise ValueError("invalid_model_asset_path")
    found = resolved(root / path)
    if not found.is_relative_to(resolved(root)):
        raise ValueError("model_asset_outside_cache")
    return found


def _file_names(directory: Path) -> tuple[str, ...]:
    found = tuple(
        sorted(
            path.relative_to(directory).as_posix()
            for path in directory.rglob("*")
            if path.is_file()
            and not any(part.startswith(".") for part in path.relative_to(directory).parts)
        )
    )
    if not found or len(found) > _MAX_FILES:
        raise ValueError("invalid_model_asset_count")
    return found


def _entry(root: Path, directory: Path, name: str) -> Asset:
    path = within(root, str(directory.relative_to(resolved(root)) / name))
    before = path.stat()
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    after = path.stat()
    if (before.st_size, before.st_mtime_ns) != (after.st_size, after.st_mtime_ns):
        raise ValueError("model_asset_changed_during_validation")
    return Asset(name, after.st_size, after.st_mtime_ns, digest.hexdigest())


def capture(root: Path, directory: Path, model: str, dimensions: int) -> Assets:
    directory = resolved(directory)
    relative = directory.relative_to(resolved(root)).as_posix()
    files = tuple(_entry(root, directory, name) for name in _file_names(directory))
    return Assets(model, dimensions, relative, files)


def _asset(raw: Any) -> Asset:
    if not isinstance(raw, dict) or set(raw) != {"path", "size", "mtime_ns", "sha256"}:
        raise ValueError("invalid_model_asset_entry")
    if not isinstance(raw["path"], str) or not isinstance(raw["sha256"], str):
        raise ValueError("invalid_model_asset_entry")
    if any(type(raw[key]) is not int or raw[key] < 0 for key in ("size", "mtime_ns")):
        raise ValueError("invalid_model_asset_stat")
    if len(raw["sha256"]) != 64 or any(ch not in "0123456789abcdef" for ch in raw["sha256"]):
        raise ValueError("invalid_model_asset_digest")
    return Asset(**raw)


def read(root: Path, model: str, dimensions: int, *, check_stat: bool = True) -> Assets | None:
    path = within(root, "manifest.json")
    if not path.exists():
        return None
    if path.stat().st_size > _MAX_MANIFEST:
        raise ValueError("model_manifest_too_large")
    raw = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(raw, dict) or raw.get("contract") != MANIFEST_VERSION:
        raise ValueError("invalid_model_manifest")
    if (
        raw.get("model") != model
        or type(raw.get("dimensions")) is not int
        or raw["dimensions"] != dimensions
    ):
        raise ValueError("model_manifest_identity_mismatch")
    if not isinstance(raw.get("directory"), str) or not isinstance(raw.get("files"), list):
        raise ValueError("invalid_model_manifest")
    directory = within(root, raw["directory"])
    items = tuple(_asset(item) for item in raw["files"])
    if tuple(item.path for item in items) != _file_names(directory):
        raise ValueError("model_asset_set_changed")
    for item in items:
        state = within(root, str(directory.relative_to(resolved(root)) / item.path)).stat()
        if check_stat and (state.st_size != item.size or state.st_mtime_ns != item.mtime_ns):
            raise ValueError("model_asset_stat_changed")
    return Assets(model, dimensions, raw["directory"], items)


def verify(root: Path, expected: Assets) -> Assets:
    actual = capture(root, within(root, expected.directory), expected.model, expected.dimensions)
    if actual.version != expected.version:
        raise ValueError("model_asset_checksum_mismatch")
    return actual


def write(root: Path, assets: Assets) -> None:
    root.mkdir(parents=True, exist_ok=True)
    content = json.dumps(
        {"contract": MANIFEST_VERSION, **asdict(assets)}, ensure_ascii=False, sort_keys=True
    )
    fd, temporary = tempfile.mkstemp(prefix=".manifest-", suffix=".json", dir=root)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as stream:
            stream.write(content)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, root / "manifest.json")
    finally:
        Path(temporary).unlink(missing_ok=True)
