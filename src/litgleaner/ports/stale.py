"""过期诊断只读取一次冻结快照，不补齐或重算历史事实。"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from litgleaner.domain.embedding import PaperEmbedding
from litgleaner.domain.identity import Alias
from litgleaner.domain.outline import TopicOutline
from litgleaner.domain.paper import Paper
from litgleaner.domain.paper_relation import PaperRelation, RelateBatch
from litgleaner.domain.qa import Answer, Question
from litgleaner.domain.reading import ReadingState
from litgleaner.domain.topic_map import TopicMap
from litgleaner.ports.translations import CachedTranslation

if TYPE_CHECKING:
    from litgleaner.ports.store import (
        Assessment,
        Extraction,
        FullTextState,
        ScoredAssessment,
        TopicState,
        TriageResult,
    )


@dataclass(frozen=True)
class StaleAssessment:
    record: Assessment
    model: str
    prompt_version: str
    created_at: str


@dataclass(frozen=True)
class StaleTranslation:
    cache: CachedTranslation
    paper_id: str | None


@dataclass(frozen=True)
class StaleSnapshot:
    papers: tuple[Paper, ...]
    fulltexts: Mapping[str, FullTextState]
    aliases: Mapping[str, tuple[Alias, ...]]
    reading: Mapping[str, ReadingState]
    extractions: tuple[Extraction, ...]
    assessments: tuple[StaleAssessment, ...]
    scores: tuple[ScoredAssessment, ...]
    translations: tuple[StaleTranslation, ...]
    questions: tuple[Question, ...]
    answers: tuple[Answer, ...]
    ask_jobs: Mapping[str, Mapping[str, Any]]
    topic_states: tuple[TopicState, ...]
    triages: tuple[TriageResult, ...]
    maps: tuple[TopicMap, ...]
    outlines: tuple[TopicOutline, ...]
    embeddings: tuple[PaperEmbedding, ...] = ()
    embedding_library_ids: frozenset[str] = frozenset()
    relations: tuple[PaperRelation, ...] = ()
    relate_batches: tuple[RelateBatch, ...] = ()
    library_paper_ids: frozenset[str] = frozenset()
    user_decided_pairs: frozenset[tuple[str, str]] = frozenset()
    user_decided_relation_ids: frozenset[str] = frozenset()
