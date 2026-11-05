"""本地文件存储。写入先落临时文件再替换，中途崩溃不会留下半个文件。"""

import os
import tempfile
from pathlib import Path, PurePosixPath


class LocalBlobStore:
    def __init__(self, root: Path) -> None:
        self._root = root.resolve()

    def _path(self, key: str) -> Path:
        relative = PurePosixPath(key)
        if not key or relative.is_absolute() or ".." in relative.parts:
            raise ValueError(f"非法的存储键：{key}")
        return self._root.joinpath(*relative.parts)

    def put(self, key: str, content: bytes) -> None:
        path = self._path(key)
        path.parent.mkdir(parents=True, exist_ok=True)
        descriptor, temporary = tempfile.mkstemp(dir=path.parent, prefix=".tmp-")
        try:
            with os.fdopen(descriptor, "wb") as handle:
                handle.write(content)
            os.replace(temporary, path)
        except BaseException:
            Path(temporary).unlink(missing_ok=True)
            raise

    def get(self, key: str) -> bytes:
        return self._path(key).read_bytes()

    def exists(self, key: str) -> bool:
        return self._path(key).is_file()

    def delete(self, key: str) -> None:
        self._path(key).unlink(missing_ok=True)
