"""找到本机 Zotero 数据目录（包含 zotero.sqlite 的目录）。

顺序：选项 data_dir → Zotero 配置文件 prefs.js 里的 extensions.zotero.dataDir → 默认 ~/Zotero。
prefs.js 里 useDataDir 为 false 时，Zotero 自己也会忽略 dataDir，这里保持一致。
"""

import json
import os
import re
import sys
from collections.abc import Iterator, Mapping
from pathlib import Path

from litgleaner.domain.errors import ConfigError

DB_NAME = "zotero.sqlite"
_PREF = re.compile(r'user_pref\(\s*"extensions\.zotero\.(dataDir|useDataDir)"\s*,\s*(.+?)\s*\)\s*;')


def discover_data_dir(
    explicit: Path | None,
    *,
    home: Path | None = None,
    platform: str = sys.platform,
    environ: Mapping[str, str] = os.environ,
) -> Path:
    if explicit is not None:
        path = Path(explicit).expanduser()
        if _has_database(path):
            return path
        raise ConfigError(f"Zotero data_dir {path} 下没有 {DB_NAME}，请检查选项 data_dir")
    home = home if home is not None else Path.home()
    for candidate in (*_profile_data_dirs(home, platform, environ), home / "Zotero"):
        if _has_database(candidate):
            return candidate
    raise ConfigError(
        f"找不到 Zotero 数据目录（应包含 {DB_NAME}）。请在 Zotero 设置 → 高级 → 文件和文件夹 "
        "里查看“数据存储位置”，并把它写进该来源的选项 data_dir；"
        "在服务器上运行时请改用 mode: web"
    )


def _has_database(path: Path) -> bool:
    return (path / DB_NAME).is_file()


def _profiles_root(home: Path, platform: str, environ: Mapping[str, str]) -> Path | None:
    if platform == "darwin":
        return home / "Library" / "Application Support" / "Zotero" / "Profiles"
    if platform.startswith("win"):
        appdata = environ.get("APPDATA")
        return Path(appdata) / "Zotero" / "Zotero" / "Profiles" if appdata else None
    return home / ".zotero" / "zotero"


def _profile_data_dirs(home: Path, platform: str, environ: Mapping[str, str]) -> Iterator[Path]:
    root = _profiles_root(home, platform, environ)
    if root is None or not root.is_dir():
        return
    for prefs in sorted(root.glob("*/prefs.js")):
        data_dir = _data_dir_pref(prefs)
        if data_dir is not None:
            yield data_dir


def _data_dir_pref(prefs: Path) -> Path | None:
    try:
        text = prefs.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return None
    values = dict(_PREF.findall(text))
    if values.get("useDataDir", "true").strip() == "false" or "dataDir" not in values:
        return None
    try:
        value = json.loads(values["dataDir"])  # prefs.js 的字符串字面量与 JSON 转义一致
    except ValueError:
        return None
    return Path(value).expanduser() if isinstance(value, str) and value else None
