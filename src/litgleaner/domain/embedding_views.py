"""向量预览、执行结果和原始相似度信号；不代表已核实关系。"""

from dataclasses import dataclass

from litgleaner.domain.embedding import EmbeddingModel


@dataclass(frozen=True)
class EmbeddingWarning:
    paper_id: str
    code: str
    detail: str


@dataclass(frozen=True)
class EmbeddingInput:
    paper_id: str
    input_text: str
    input_hash: str
    source_extraction_id: int | None
    warnings: tuple[EmbeddingWarning, ...]


@dataclass(frozen=True)
class ReembedPlan:
    model: EmbeddingModel | None
    total: int
    cached: int
    pending_paper_ids: tuple[str, ...]
    skipped_paper_ids: tuple[str, ...]
    warnings: tuple[EmbeddingWarning, ...]


@dataclass(frozen=True)
class ReembedResult:
    plan: ReembedPlan
    created: int
    reused: int
    changed_paper_ids: tuple[str, ...]


@dataclass(frozen=True)
class SimilarPaper:
    paper_id: str
    title: str
    cosine: float


@dataclass(frozen=True)
class SimilarPapers:
    paper_id: str
    model: EmbeddingModel | None
    matches: tuple[SimilarPaper, ...]
    total: int
    warnings: tuple[EmbeddingWarning, ...]
