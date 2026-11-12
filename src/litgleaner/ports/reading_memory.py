"""阅读记忆摘要的只读一致快照；领域转换与原页核验在事务外进行。"""

from dataclasses import dataclass

from litgleaner.domain.paper_relation import PaperRelation, RelateBatch
from litgleaner.domain.reading import Annotation
from litgleaner.ports.memory import MemorySnapshot


@dataclass(frozen=True)
class ReadingMemorySnapshot:
    facts: MemorySnapshot
    relations: tuple[PaperRelation, ...]
    batches: tuple[RelateBatch, ...]
    annotations: tuple[Annotation, ...]
    user_decided_relation_ids: frozenset[str]
