"""数据访问接口。目前只有 SQLite 一种实现，接口用于维持依赖方向。"""

from collections.abc import Iterable, Mapping
from contextlib import AbstractContextManager
from dataclasses import dataclass
from typing import Any, Literal, Protocol

from litfactory.domain.identity import Alias
from litfactory.domain.paper import Paper
from litfactory.domain.rubric import ScoreResult

FullTextStatus = Literal["available", "parsed", "unavailable", "failed"]
DeliveryStatus = Literal["pending", "sending", "sent", "failed"]
JobStatus = Literal["pending", "done", "dead"]


@dataclass(frozen=True)
class JobSpec:
    key: str
    stage: str
    payload: Mapping[str, Any]


@dataclass(frozen=True)
class Job:
    key: str
    stage: str
    payload: Mapping[str, Any]
    status: str
    attempts: int
    last_error: str | None
    first_deferred_at: str | None = None


@dataclass(frozen=True)
class FullTextState:
    paper_id: str
    status: FullTextStatus
    pdf_sha256: str | None = None
    doc_key: str | None = None
    provider: str | None = None
    license_basis: str | None = None
    quality: str | None = None


@dataclass(frozen=True)
class Artifact:
    sha256: str
    kind: str
    blob_key: str
    size: int
    producer: str
    license_basis: str | None


@dataclass(frozen=True)
class Extraction:
    id: int
    paper_id: str
    input_hash: str
    extractor_version: str
    data: Mapping[str, Any]


@dataclass(frozen=True)
class TopicState:
    topic_id: str
    status: str
    topic_hash: str
    triage_from: str


@dataclass(frozen=True)
class TriageResult:
    paper_id: str
    topic_id: str
    topic_hash: str
    relevant: bool
    reason: str
    prompt_version: str
    model: str


@dataclass(frozen=True)
class NewAssessment:
    paper_id: str
    topic_id: str
    topic_hash: str
    semantic_hash: str
    input_hash: str
    levels: Mapping[str, int | None]
    evidence: Mapping[str, Any]
    fulltext_used: bool
    model: str
    prompt_version: str


@dataclass(frozen=True)
class Assessment:
    id: int
    paper_id: str
    topic_id: str
    topic_hash: str
    semantic_hash: str
    input_hash: str
    levels: Mapping[str, int | None]
    evidence: Mapping[str, Any]
    fulltext_used: bool


@dataclass(frozen=True)
class ScoredAssessment:
    assessment: Assessment
    policy_hash: str
    score: int
    tier: str
    provisional: bool


@dataclass(frozen=True)
class RankQuery:
    topic_id: str
    topic_hash: str
    semantic_hash: str
    policy_hash: str
    tiers: tuple[str, ...]
    channel: str
    limit: int


@dataclass(frozen=True)
class DigestRecord:
    id: str
    route_id: str
    topic_id: str
    channel: str
    period: str


@dataclass(frozen=True)
class FeedbackRecord:
    id: str
    paper_id: str
    channel: str
    action: str
    digest_id: str | None = None
    actor: str | None = None
    external_id: str | None = None


@dataclass(frozen=True)
class LibraryRecord:
    paper_id: str
    library: str
    item_key: str
    created: bool
    warnings: tuple[str, ...] = ()


@dataclass(frozen=True)
class LLMCall:
    task: str
    model: str
    prompt_version: str
    input_tokens: int
    output_tokens: int
    cost_usd: float
    ok: bool


class PaperRepo(Protocol):
    def find_ids_by_aliases(self, aliases: Iterable[Alias]) -> frozenset[str]: ...
    def find_by_title_key(self, key: str) -> tuple[Paper, ...]: ...
    def insert(self, paper: Paper) -> None: ...
    def fill_missing(self, paper_id: str, fields: Mapping[str, str | None]) -> None: ...
    def add_aliases(self, paper_id: str, aliases: Iterable[Alias]) -> None: ...
    def log_merge(
        self, paper_id: str, kind: str, status: str, detail: Mapping[str, Any], now: str
    ) -> None: ...
    def record_sighting(
        self, paper_id: str, source: str, query_hash: str, raw_key: str | None, now: str
    ) -> None: ...
    def get(self, paper_id: str) -> Paper | None: ...
    def aliases_of(self, paper_id: str) -> tuple[Alias, ...]: ...
    def get_cursor(self, source: str, query_hash: str) -> str | None: ...
    def set_cursor(self, source: str, query_hash: str, cursor: str | None, now: str) -> None: ...
    def source_contribution(self) -> dict[str, int]: ...
    def get_fulltext(self, paper_id: str) -> FullTextState | None: ...
    def save_fulltext(self, state: FullTextState, now: str) -> None: ...
    def papers_with_fulltext_status(self, status: FullTextStatus) -> tuple[str, ...]: ...
    def add_artifact(self, artifact: Artifact, now: str) -> None: ...
    def get_extraction(self, paper_id: str) -> Extraction | None: ...
    def insert_extraction(
        self,
        paper_id: str,
        input_hash: str,
        extractor_version: str,
        data: Mapping[str, Any],
        now: str,
    ) -> None: ...
    def parsed_without_extraction(self) -> tuple[str, ...]: ...


class JudgmentRepo(Protocol):
    def get_topic_state(self, topic_id: str) -> TopicState | None: ...
    def save_topic_state(self, state: TopicState, now: str) -> None: ...
    def topic_states(self) -> tuple[TopicState, ...]: ...
    def papers_awaiting_triage(
        self, topic_id: str, topic_hash: str, seen_from: str
    ) -> tuple[str, ...]: ...
    def untriaged_papers(
        self, topic_id: str, topic_hash: str, since: str | None
    ) -> tuple[str, ...]: ...
    def save_triage(self, result: TriageResult, now: str) -> None: ...
    def relevant_papers(
        self, topic_id: str, topic_hash: str, since: str | None = None
    ) -> tuple[str, ...]: ...
    def relevant_without_fulltext(self, topic_id: str, topic_hash: str) -> tuple[str, ...]: ...
    def assessment_keys(self, paper_id: str, topic_id: str) -> tuple[tuple[str, str, str], ...]: ...
    def insert_assessment(self, assessment: NewAssessment, now: str) -> None: ...
    def get_assessment(self, assessment_id: int) -> Assessment | None: ...
    def unscored_assessments(
        self, topic_id: str, semantic_hash: str, policy_hash: str
    ) -> tuple[int, ...]: ...
    def save_score(
        self, assessment_id: int, policy_hash: str, result: ScoreResult, now: str
    ) -> None: ...
    def save_rubric_snapshot(self, hash_: str, rubric_id: str, body: str, now: str) -> None: ...
    def rank_candidates(self, query: RankQuery) -> tuple[ScoredAssessment, ...]: ...
    def scored_for_paper(self, paper_id: str) -> tuple[ScoredAssessment, ...]: ...
    def papers_with_scores(self) -> tuple[str, ...]: ...


class OutputRepo(Protocol):
    def digest_exists(self, route_id: str, topic_id: str, period: str) -> bool: ...
    def create_digest(
        self, digest: DigestRecord, items: Iterable[tuple[str, Mapping[str, Any]]], now: str
    ) -> None: ...
    def get_digest(self, digest_id: str) -> DigestRecord | None: ...
    def digest_cards(self, digest_id: str) -> tuple[Mapping[str, Any], ...]: ...
    def undelivered(self) -> tuple[tuple[str, str], ...]: ...
    def unresolved(self) -> tuple[tuple[str, str], ...]: ...
    def claim_delivery(self, digest_id: str, channel: str, now: str) -> bool: ...
    def delivery_status(self, digest_id: str, channel: str) -> str | None: ...
    def set_delivery(
        self,
        digest_id: str,
        channel: str,
        status: DeliveryStatus,
        now: str,
        *,
        external_id: str | None = None,
        error: str | None = None,
    ) -> None: ...


class FeedbackRepo(Protocol):
    def add(self, record: FeedbackRecord, now: str) -> bool: ...
    def pending_saves(self, library: str) -> tuple[str, ...]: ...
    def latest_action(self, paper_id: str) -> str | None: ...
    def save_entry(self, record: LibraryRecord, now: str) -> None: ...
    def get_entry(self, paper_id: str, library: str) -> LibraryRecord | None: ...
    def latest_card(self, paper_id: str) -> tuple[Mapping[str, Any], str] | None: ...


class JobRepo(Protocol):
    def enqueue(self, spec: JobSpec, now: str, *, reopen: bool = False) -> bool: ...
    def reopen(self, key: str, now: str) -> None: ...
    def due(self, stage: str, now: str) -> tuple[Job, ...]: ...
    def mark_done(self, key: str, now: str) -> None: ...
    def mark_retry(self, key: str, error: str, next_run_at: str, now: str) -> None: ...
    def postpone(self, key: str, error: str, next_run_at: str, now: str) -> None: ...
    def mark_dead(self, key: str, error: str, now: str) -> None: ...
    def list_by_status(self, status: JobStatus) -> tuple[Job, ...]: ...
    def retry_dead(self, now: str) -> int: ...


class RunRepo(Protocol):
    def record_llm_call(self, call: LLMCall, now: str) -> None: ...
    def llm_cost_since(self, since: str) -> float: ...
    def llm_call_count(self, task: str | None = None) -> int: ...
    def average_llm_cost(self, task: str) -> float | None: ...


class Store(Protocol):
    @property
    def papers(self) -> PaperRepo: ...
    @property
    def judgments(self) -> JudgmentRepo: ...
    @property
    def outputs(self) -> OutputRepo: ...
    @property
    def feedback(self) -> FeedbackRepo: ...
    @property
    def jobs(self) -> JobRepo: ...
    @property
    def runs(self) -> RunRepo: ...

    def atomic(self) -> AbstractContextManager[None]: ...
    def stats(self) -> dict[str, int]: ...
