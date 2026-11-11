"""课题地图是模型判断；论文身份只来自明确发送的输入。"""

from collections.abc import Iterable, Mapping
from typing import Annotated, Any

from pydantic import Field, field_validator, model_validator

from litgleaner.domain.base import FrozenModel
from litgleaner.domain.language import LanguageCode

PaperID = Annotated[str, Field(min_length=1, max_length=200, pattern=r"\S")]


class TopicMapSettings(FrozenModel):
    token_budget: int = Field(default=12000, strict=True, ge=512, le=64000)


class MapGroup(FrozenModel):
    name: str = Field(min_length=1, max_length=120)
    description: str = Field(min_length=1, max_length=800)
    paper_ids: tuple[PaperID, ...] = Field(min_length=1)

    @field_validator("name", "description")
    @classmethod
    def _nonblank(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("map_group_text_empty")
        return value.strip()


class TopicMap(FrozenModel):
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
    groups: tuple[MapGroup, ...] = Field(max_length=8)
    ungrouped: tuple[PaperID, ...]
    warnings: tuple[str, ...] = ()

    @model_validator(mode="after")
    def _partition(self) -> "TopicMap":
        all_ids, sent_ids = set(self.considered_paper_ids), set(self.input_paper_ids)
        assigned = [id_ for group in self.groups for id_ in group.paper_ids]
        if len(all_ids) != len(self.considered_paper_ids) or len(sent_ids) != len(
            self.input_paper_ids
        ):
            raise ValueError("map_duplicate_input")
        if not sent_ids <= all_ids or set(self.source_hashes) != sent_ids:
            raise ValueError("map_input_not_considered")
        if any(not value.strip() for value in self.source_hashes.values()):
            raise ValueError("map_source_hash_empty")
        if len(set(assigned)) != len(assigned) or not set(assigned) <= sent_ids:
            raise ValueError("map_invalid_assignment")
        if set(self.ungrouped) != all_ids - set(assigned) or len(set(self.ungrouped)) != len(
            self.ungrouped
        ):
            raise ValueError("map_invalid_ungrouped")
        return self


def validate_groups(
    raw: Iterable[Mapping[str, Any]], input_paper_ids: tuple[str, ...]
) -> tuple[tuple[MapGroup, ...], tuple[str, ...]]:
    """去除未知编号、重复归组与空分组，不为错误输出伪造新分组。"""
    allowed, assigned = frozenset(input_paper_ids), set()
    groups: list[MapGroup] = []
    warnings: list[str] = []
    for item in raw:
        ids: list[str] = []
        for id_ in item["paper_ids"]:
            if id_ not in allowed:
                warnings.append("模型输出含未发送的论文编号，已丢弃")
            elif id_ in assigned:
                warnings.append("模型输出重复归组，保留首次归属")
            else:
                ids.append(id_)
                assigned.add(id_)
        name, description = item["name"].strip(), item["description"].strip()
        if ids and name and description:
            groups.append(MapGroup(name=name, description=description, paper_ids=tuple(ids)))
        else:
            assigned.difference_update(ids)
            warnings.append("模型输出的空分组或空说明已丢弃")
    return tuple(groups), tuple(dict.fromkeys(warnings))
