"""个人数据导出的全量事实快照；不包含文件读取或格式渲染。"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

from litgleaner.domain.identity import Alias
from litgleaner.domain.knowledge import Concept, ConceptAlias, ConceptOccurrence
from litgleaner.domain.paper import Paper
from litgleaner.domain.qa import Answer, Question
from litgleaner.domain.reading import Annotation, ReadingState


@dataclass(frozen=True)
class ExportPaper:
    paper: Paper
    aliases: tuple[Alias, ...]
    reading: ReadingState | None
    annotations: tuple[Annotation, ...]
    extractions: tuple[Mapping[str, Any], ...]
    assessments: tuple[Mapping[str, Any], ...]
    questions: tuple[Question, ...]
    answers: tuple[Answer, ...]
    pdf_sha256: str | None


@dataclass(frozen=True)
class ExportConcept:
    concept: Concept
    aliases: tuple[ConceptAlias, ...]
    occurrences: tuple[ConceptOccurrence, ...]


@dataclass(frozen=True)
class ExportSnapshot:
    papers: tuple[ExportPaper, ...]
    concepts: tuple[ExportConcept, ...]
