"""按目录自动打测试层标记。"""

from pathlib import Path

import pytest

_LAYERS = ("unit", "contracts", "integration", "e2e")


def pytest_collection_modifyitems(items: list[pytest.Item]) -> None:
    for item in items:
        parts = Path(str(item.path)).parts
        for layer in _LAYERS:
            if layer in parts:
                item.add_marker(layer)
