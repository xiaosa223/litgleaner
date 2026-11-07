"""假文献库：记录写入的条目，可以注入失败，演示和测试用。"""

from litgleaner.domain.library import LibraryEntry
from litgleaner.ports.library import LibraryReceipt


class FakeLibrary:
    name = "fake_library"

    def __init__(self) -> None:
        self.saved: list[LibraryEntry] = []
        self.fail_next: list[Exception] = []

    def save(self, entry: LibraryEntry) -> LibraryReceipt:
        if self.fail_next:
            raise self.fail_next.pop(0)
        known = [i for i, item in enumerate(self.saved) if item.key == entry.key]
        if known:
            return LibraryReceipt(item_key=f"ITEM{known[0]}", created=False)
        self.saved.append(entry)
        return LibraryReceipt(item_key=f"ITEM{len(self.saved) - 1}", created=True)
