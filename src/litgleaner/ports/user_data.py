"""用户自己的修正和决定：元数据修正（ADR 0011）、概念关系与合并的确认（第 12.7 节）。"""

from typing import Protocol

from litgleaner.ports.concept_links import ConceptLinkStore
from litgleaner.ports.corrections import CorrectionStore


class UserDataStore(CorrectionStore, ConceptLinkStore, Protocol):
    pass
