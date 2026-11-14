"""用户修正的论文元数据（ADR 0011），只追加。"""

from collections.abc import Iterable, Mapping
from typing import Protocol

from litgleaner.domain.correction import Correction


class CorrectionRepo(Protocol):
    def add(self, item: Correction) -> None: ...
    def latest(self, paper_ids: Iterable[str]) -> Mapping[str, Mapping[str, str]]: ...


class CorrectionStore(Protocol):
    @property
    def corrections(self) -> CorrectionRepo: ...
