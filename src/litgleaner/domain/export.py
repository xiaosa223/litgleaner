"""开放格式导出的包内文件；名称不代表本机文件系统路径。"""

from dataclasses import dataclass
from hashlib import sha256


@dataclass(frozen=True)
class ExportFile:
    name: str
    content: bytes


def export_name(kind: str, identifier: str, suffix: str = "md") -> str:
    """只用稳定哈希命名，论文标题、标识符和概念名不进入路径。"""
    return f"{kind}/{sha256(identifier.encode()).hexdigest()}.{suffix}"
