"""文献库接口：把选中的论文写进用户自己的文献管理软件。"""

from dataclasses import dataclass
from typing import Protocol

from litgleaner.domain.library import LibraryEntry


@dataclass(frozen=True)
class LibraryReceipt:
    item_key: str
    created: bool  # False 表示库里已经有这篇，只补了分组和标签
    warnings: tuple[str, ...] = ()


class ReferenceLibrary(Protocol):
    """软件没开、连不上时抛 Deferred 或 TransientError；未授权抛 AuthFailed。

    同一篇论文重复调用不能产生重复条目：实现要先按 DOI 等标识符或标题查重。
    """

    name: str

    def save(self, entry: LibraryEntry) -> LibraryReceipt: ...
