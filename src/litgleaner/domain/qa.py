"""论文问答的用户原文与有出处的模型回答。页码统一零基。"""

from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Any, Literal

from litgleaner.domain.language import LanguageCode


@dataclass(frozen=True)
class Question:
    id: str
    paper_id: str
    request_id: str
    text: str
    selection: str | None
    page: int | None
    reading_language: LanguageCode
    created_at: str


@dataclass(frozen=True)
class Citation:
    paper_id: str
    page: int
    quote: str


@dataclass(frozen=True)
class NoteCitation:
    paper_id: str
    source_id: str
    quote: str
    kind: Literal["note", "question"] = "note"


@dataclass(frozen=True)
class Statement:
    text: str
    citations: tuple[Citation, ...]
    inference: bool
    note_citations: tuple[NoteCitation, ...] = ()
    mentioned_paper_ids: tuple[str, ...] = ()


@dataclass(frozen=True)
class Answer:
    id: str
    paper_id: str
    question_id: str
    statements: tuple[Statement, ...]
    model: str
    producer_version: str
    input_hash: str
    created_at: str
    context: Mapping[str, Any] = field(default_factory=dict)
    request_producer_version: str | None = None
