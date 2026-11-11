"""论文问答的用户原文与有出处的模型回答。页码统一零基。"""

from dataclasses import dataclass

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
class Statement:
    text: str
    citations: tuple[Citation, ...]
    inference: bool


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
