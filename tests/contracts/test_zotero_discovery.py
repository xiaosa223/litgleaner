"""Zotero 本地数据目录发现：选项 data_dir → 各平台 prefs.js 的 dataDir → ~/Zotero。"""

import json
from pathlib import Path

import pytest

from litgleaner.adapters.zotero.discovery import discover_data_dir
from litgleaner.domain.errors import ConfigError


def write_prefs(profiles: Path, data_dir: str, *, use: bool = True) -> None:
    folder = profiles / "abc123.default"
    folder.mkdir(parents=True)
    lines = [
        'user_pref("extensions.zotero.lastViewedFolder", "L1");',
        f'user_pref("extensions.zotero.dataDir", {json.dumps(data_dir)});',
        f'user_pref("extensions.zotero.useDataDir", {str(use).lower()});',
    ]
    (folder / "prefs.js").write_text("\n".join(lines), encoding="utf-8")


def make_data_dir(path: Path) -> Path:
    path.mkdir(parents=True)
    (path / "zotero.sqlite").write_bytes(b"")
    return path


def test_discovers_data_dir_from_profile_prefs(tmp_path: Path):
    custom = make_data_dir(tmp_path / "Research Data" / "Zotero")
    make_data_dir(tmp_path / "home" / "Zotero")
    home = tmp_path / "home"
    write_prefs(home / "Library/Application Support/Zotero/Profiles", str(custom))
    assert discover_data_dir(None, home=home, platform="darwin", environ={}) == custom
    linux_home = tmp_path / "linux"
    write_prefs(linux_home / ".zotero/zotero", str(custom))
    assert discover_data_dir(None, home=linux_home, platform="linux", environ={}) == custom
    appdata = tmp_path / "AppData" / "Roaming"
    write_prefs(appdata / "Zotero/Zotero/Profiles", str(custom))
    env = {"APPDATA": str(appdata)}
    assert discover_data_dir(None, home=tmp_path / "w", platform="win32", environ=env) == custom


def test_discovery_prefers_explicit_then_falls_back_to_home(tmp_path: Path):
    home = tmp_path / "home"
    default = make_data_dir(home / "Zotero")
    explicit = make_data_dir(tmp_path / "explicit")
    write_prefs(home / ".zotero/zotero", str(tmp_path / "ignored"), use=False)
    assert discover_data_dir(explicit, home=home, platform="linux", environ={}) == explicit
    assert discover_data_dir(None, home=home, platform="linux", environ={}) == default


def test_discovery_failure_explains_data_dir(tmp_path: Path):
    with pytest.raises(ConfigError, match="data_dir"):
        discover_data_dir(None, home=tmp_path, platform="darwin", environ={})
