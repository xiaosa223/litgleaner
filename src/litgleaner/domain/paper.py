"""论文事实：外部记录进入系统的边界，以及库内的论文实体。"""

from collections.abc import Mapping
from dataclasses import dataclass
from datetime import date

from pydantic import Field, field_validator

from litgleaner.domain.base import FrozenModel


class SourceRecord(FrozenModel):
    """信号源返回的一条记录。所有外部数据都必须先通过这里的校验。"""

    title: str = Field(min_length=1)
    abstract: str | None = None
    authors: tuple[str, ...] = ()
    venue: str | None = None
    issn: str | None = None
    published_at: date | None = None
    doi: str | None = None
    arxiv_id: str | None = None
    external_ids: Mapping[str, str] = Field(default_factory=dict)

    @field_validator("title")
    @classmethod
    def _title_not_blank(cls, value: str) -> str:
        stripped = value.strip()
        if not stripped:
            raise ValueError("标题不能为空")
        return stripped


@dataclass(frozen=True)
class Paper:
    id: str
    title: str
    title_key: str
    abstract: str | None
    authors: tuple[str, ...]
    venue: str | None
    issn: str | None
    published_at: str | None
    first_seen_at: str
