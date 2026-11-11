"""综述骨架只保存已确认输入上的短分组判断及代码生成的事实附件。"""

from collections.abc import Mapping
from typing import Annotated, Any

from pydantic import Field, ValidationError, field_validator, model_validator

from litgleaner.domain.base import FrozenModel
from litgleaner.domain.language import LanguageCode

PaperID = Annotated[str, Field(min_length=1, max_length=200, pattern=r"\S")]
GoalID = Annotated[str, Field(pattern=r"^[A-Za-z0-9_-]+$")]


class TopicOutlineSettings(FrozenModel):
    token_budget: int = Field(default=12000, strict=True, ge=512, le=64000)


class OutlineGroup(FrozenModel):
    name: str = Field(min_length=1, max_length=120)
    description: str = Field(min_length=1, max_length=600)
    goal_ids: tuple[GoalID, ...]
    paper_ids: tuple[PaperID, ...] = Field(min_length=1)

    @field_validator("name", "description")
    @classmethod
    def _nonblank(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("outline_group_text_empty")
        return value.strip()

    @model_validator(mode="after")
    def _unique(self) -> "OutlineGroup":
        if len(set(self.goal_ids)) != len(self.goal_ids) or len(set(self.paper_ids)) != len(
            self.paper_ids
        ):
            raise ValueError("outline_group_duplicate_ids")
        return self


class OutlineGap(FrozenModel):
    description: str = Field(min_length=1, max_length=500)
    paper_ids: tuple[PaperID, ...] = Field(min_length=1)

    @field_validator("description")
    @classmethod
    def _nonblank(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("outline_gap_text_empty")
        return value.strip()

    @model_validator(mode="after")
    def _unique(self) -> "OutlineGap":
        if len(set(self.paper_ids)) != len(self.paper_ids):
            raise ValueError("outline_gap_duplicate_ids")
        return self


class OutlinePaper(FrozenModel):
    paper_id: PaperID
    title: str
    table_markdown: str
    bibtex: str
    csl_json: str
    needs_verification: bool = Field(strict=True)
    reference_warnings: tuple[str, ...] = ()


class TopicOutline(FrozenModel):
    id: str
    topic_id: str
    topic_hash: str
    input_hash: str
    producer_version: str
    model: str
    reading_language: LanguageCode
    token_budget: int = Field(strict=True, ge=512, le=64000)
    created_at: str
    scope_id: str
    considered_paper_ids: tuple[PaperID, ...]
    input_paper_ids: tuple[PaperID, ...]
    source_hashes: dict[str, str]
    goal_ids: tuple[GoalID, ...]
    groups: tuple[OutlineGroup, ...] = Field(max_length=12)
    gaps: tuple[OutlineGap, ...] = Field(max_length=12)
    papers: tuple[OutlinePaper, ...]
    ungrouped: tuple[PaperID, ...]
    warnings: tuple[str, ...] = ()

    @model_validator(mode="after")
    def _partition(self) -> "TopicOutline":
        considered, sent = set(self.considered_paper_ids), set(self.input_paper_ids)
        if len(considered) != len(self.considered_paper_ids) or len(sent) != len(
            self.input_paper_ids
        ):
            raise ValueError("outline_duplicate_input")
        if not sent <= considered or set(self.source_hashes) != sent:
            raise ValueError("outline_input_not_considered")
        if any(not value.strip() for value in self.source_hashes.values()):
            raise ValueError("outline_source_hash_empty")
        assigned = [id_ for group in self.groups for id_ in group.paper_ids]
        if len(set(assigned)) != len(assigned) or not set(assigned) <= sent:
            raise ValueError("outline_invalid_assignment")
        if set(self.ungrouped) != considered - set(assigned) or len(set(self.ungrouped)) != len(
            self.ungrouped
        ):
            raise ValueError("outline_invalid_ungrouped")
        if tuple(paper.paper_id for paper in self.papers) != self.input_paper_ids:
            raise ValueError("outline_invalid_attachments")
        goals = set(self.goal_ids)
        if len(goals) != len(self.goal_ids) or any(
            not set(group.goal_ids) <= goals for group in self.groups
        ):
            raise ValueError("outline_invalid_goals")
        if any(not set(gap.paper_ids) <= sent for gap in self.gaps):
            raise ValueError("outline_invalid_gap_input")
        return self


def _items(value: Any) -> tuple[Any, ...]:
    return tuple(value) if isinstance(value, list | tuple) else ()


def _clean_ids(value: Any, allowed: frozenset[str], warnings: list[str]) -> tuple[str, ...]:
    result: list[str] = []
    for id_ in _items(value):
        if not isinstance(id_, str) or id_ not in allowed:
            warnings.append("模型输出含未知或未发送的编号，已丢弃")
        elif id_ not in result:
            result.append(id_)
        else:
            warnings.append("模型输出的重复编号已去除")
    return tuple(result)


def _groups(
    raw: Any, paper_ids: tuple[str, ...], goal_ids: tuple[str, ...], warnings: list[str]
) -> tuple[OutlineGroup, ...]:
    allowed, goals = frozenset(paper_ids), frozenset(goal_ids)
    assigned: set[str] = set()
    result: list[OutlineGroup] = []
    for item in _items(raw):
        if not isinstance(item, Mapping):
            warnings.append("模型输出的无效分组已丢弃")
            continue
        ids = _clean_ids(item.get("paper_ids"), allowed, warnings)
        if any(id_ in assigned for id_ in ids):
            warnings.append("模型输出重复归组，保留首次归属")
        ids = tuple(id_ for id_ in ids if id_ not in assigned)
        try:
            group = OutlineGroup(
                name=item.get("name"),
                description=item.get("description"),
                paper_ids=ids,
                goal_ids=_clean_ids(item.get("goal_ids"), goals, warnings),
            )
        except ValidationError:
            warnings.append("模型输出的空分组、空说明或无效文本已丢弃")
            continue
        if len(result) == 12:
            warnings.append("模型输出超过十二个分组，超出部分已丢弃")
            continue
        result.append(group)
        assigned.update(ids)
    return tuple(result)


def _gaps(
    raw: Any, input_ids: tuple[str, ...], eligible_ids: tuple[str, ...], warnings: list[str]
) -> tuple[OutlineGap, ...]:
    eligible = frozenset(input_ids) & frozenset(eligible_ids)
    result: list[OutlineGap] = []
    for item in _items(raw):
        if not isinstance(item, Mapping):
            warnings.append("模型输出的无效覆盖缺口候选已丢弃")
            continue
        ids = _items(item.get("paper_ids"))
        if any(not isinstance(id_, str) or id_ not in eligible for id_ in ids):
            warnings.append("覆盖缺口候选含未发送或证据不完整的论文，整条已丢弃")
            continue
        try:
            gap = OutlineGap(
                description=item.get("description"), paper_ids=tuple(dict.fromkeys(ids))
            )
        except ValidationError:
            warnings.append("模型输出的空覆盖缺口候选或无效文本已丢弃")
            continue
        if len(result) == 12:
            warnings.append("模型输出超过十二条覆盖缺口候选，超出部分已丢弃")
            continue
        if len(ids) != len(gap.paper_ids):
            warnings.append("覆盖缺口候选内的重复论文编号已去除")
        result.append(gap)
    return tuple(result)


def validate_outline(
    raw: Mapping[str, Any],
    input_paper_ids: tuple[str, ...],
    goal_ids: tuple[str, ...],
    eligible_gap_ids: tuple[str, ...],
) -> tuple[tuple[OutlineGroup, ...], tuple[OutlineGap, ...], tuple[str, ...]]:
    """只保留当前已读输入上的归组和有完整证据支持的覆盖缺口候选。"""
    warnings: list[str] = []
    groups = _groups(raw.get("groups"), input_paper_ids, goal_ids, warnings)
    gaps = _gaps(raw.get("gaps"), input_paper_ids, eligible_gap_ids, warnings)
    return groups, gaps, tuple(dict.fromkeys(warnings))
