"""按目录自动打测试层标记。"""

from pathlib import Path

import pytest

_LAYERS = ("unit", "contracts", "integration", "e2e")


@pytest.fixture(autouse=True)
def isolated_system_secrets(monkeypatch: pytest.MonkeyPatch) -> None:
    """默认封锁真实钥匙串；密钥合同测试按需覆盖为内存后端。"""
    monkeypatch.setattr("litgleaner.adapters.secrets.system._system_backend", lambda: None)


def pytest_collection_modifyitems(items: list[pytest.Item]) -> None:
    for item in items:
        parts = Path(str(item.path)).parts
        for layer in _LAYERS:
            if layer in parts:
                item.add_marker(layer)
