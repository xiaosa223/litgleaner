"""课题调研的有界计划、来源回执与只追加轮次事实。"""

from datetime import date
from typing import Annotated, Literal

from pydantic import Field, model_validator

from litgleaner.domain.base import FrozenModel
from litgleaner.domain.identity import Alias
from litgleaner.domain.paper import SourceRecord

SurveyMode = Literal["reviews", "highly_cited", "recent", "references", "citations"]


class SurveySettings(FrozenModel):
    rounds: int = Field(default=2, strict=True, ge=1, le=10)
    candidate_limit: int = Field(default=100, strict=True, ge=1, le=1000)
    category_limit: int = Field(default=30, strict=True, ge=1, le=1000)
    expansion_limit: int = Field(default=20, strict=True, ge=1, le=1000)
    seed_limit: int = Field(default=5, strict=True, ge=1, le=50)
    since: date = date(1900, 1, 1)
    coverage_threshold: float = Field(default=0.05, ge=0, le=1)


class SurveySource(FrozenModel):
    source: str
    mode: SurveyMode
    query_hash: str
    fetched: int = Field(ge=0, strict=True)
    truncated: bool = False
    warnings: tuple[str, ...] = ()
    error: str | None = None
    raw_key: str | None = None


class SurveyEdge(FrozenModel):
    source: str
    citing_aliases: tuple[Alias, ...]
    cited_aliases: tuple[Alias, ...]
    source_position: int = Field(ge=0, strict=True)
    raw_text: str
    contexts: tuple[str, ...] = ()
    intents: tuple[str, ...] = ()
    influential: bool | None = None


class SurveyCandidate(FrozenModel):
    key: str
    record: SourceRecord
    sources: tuple[str, ...]
    modes: tuple[SurveyMode, ...]
    referenced_by: tuple[str, ...] = ()


class SurveyPreview(FrozenModel):
    id: str
    topic_id: str
    topic_hash: str
    fingerprint: str
    created_at: str
    internal: bool = False
    settings: SurveySettings
    candidates: tuple[SurveyCandidate, ...]
    edges: tuple[SurveyEdge, ...] = ()
    sources: tuple[SurveySource, ...] = ()
    uncovered: tuple[str, ...] = ()
    triage_calls: int = Field(ge=0, strict=True)
    fulltext_candidates: int = Field(default=0, ge=0, strict=True)
    extract_calls: int = Field(default=0, ge=0, strict=True)
    assess_calls: int = Field(default=0, ge=0, strict=True)
    locally_rejected: int = Field(ge=0, strict=True)
    known_papers: int = Field(ge=0, strict=True)
    estimated_usd: float | None = Field(default=None, ge=0)
    estimated_pipeline_usd: float | None = Field(default=None, ge=0)


class SurveyWork(FrozenModel):
    run_id: str
    iteration: int = Field(ge=1, strict=True)
    plan_id: str
    scope_id: str
    topic_id: str
    topic_hash: str
    started_at: str
    paper_ids: tuple[str, ...]
    new_paper_ids: tuple[str, ...]
    paper_sources: dict[str, tuple[str, ...]]
    rejected_records: int = Field(default=0, ge=0, strict=True)

    @model_validator(mode="after")
    def _paper_sets(self) -> "SurveyWork":
        _unique_papers(self.paper_ids)
        _unique_papers(self.new_paper_ids)
        if not set(self.new_paper_ids) <= set(self.paper_ids):
            raise ValueError("survey_new_papers_not_subset")
        if not set(self.paper_sources) <= set(self.paper_ids):
            raise ValueError("survey_sources_not_subset")
        return self


class NewSurveyRound(FrozenModel):
    topic_id: str
    topic_hash: str
    run_id: str
    iteration: int = Field(ge=1, strict=True)
    plan_id: str
    started_at: str
    finished_at: str
    candidates: int = Field(ge=0, strict=True)
    new_papers: int = Field(ge=0, strict=True)
    passed: int = Field(ge=0, strict=True)
    new_passed: int = Field(ge=0, strict=True)
    undetermined: int = Field(ge=0, strict=True)
    failed_jobs: tuple[str, ...] = ()
    cost_usd: float = Field(ge=0)
    coverage: float | None = Field(default=None, ge=0, le=1)
    next_recommended: bool | None = None
    sources: tuple[SurveySource, ...] = ()
    contributions: dict[str, Annotated[int, Field(strict=True, ge=0)]] = Field(default_factory=dict)
    uncovered: tuple[str, ...] = ()
    paper_ids: tuple[str, ...] = ()
    cancelled: bool = False

    @model_validator(mode="after")
    def _counts(self) -> "NewSurveyRound":
        if not 0 <= self.new_passed <= self.passed <= self.candidates:
            raise ValueError("survey_passed_counts_invalid")
        if not self.new_passed <= self.new_papers <= self.candidates:
            raise ValueError("survey_new_counts_invalid")
        if self.undetermined > self.candidates:
            raise ValueError("survey_undetermined_invalid")
        _unique_papers(self.paper_ids)
        return self


class SurveyRound(NewSurveyRound):
    number: int = Field(ge=1, strict=True)


def _unique_papers(values: tuple[str, ...]) -> None:
    if any(not value.strip() for value in values) or len(set(values)) != len(values):
        raise ValueError("survey_paper_ids_invalid")


def coverage_ratio(new_passed: int, candidates: int, undetermined: int = 0) -> float | None:
    if min(new_passed, candidates, undetermined) < 0 or new_passed > candidates:
        raise ValueError("覆盖度计数无效")
    return new_passed / candidates if candidates and not undetermined else None
