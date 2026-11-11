"""全局概念及其论文位置；学习程度与用户评价另行记录。"""

import unicodedata
from dataclasses import dataclass
from typing import Literal

from litgleaner.domain.reading import BBox

ConceptRole = Literal["core", "prerequisite", "mentioned"]


def normalize_concept_name(name: str) -> str:
    """只统一 Unicode、大小写与空白，不猜测缩写、同义词或语义合并。"""
    return " ".join(unicodedata.normalize("NFKC", name).split()).casefold()


@dataclass(frozen=True)
class Concept:
    id: str
    name: str
    created_at: str


@dataclass(frozen=True)
class ConceptAlias:
    normalized_name: str
    concept_id: str
    name: str
    language: str | None
    created_at: str


@dataclass(frozen=True)
class ConceptOccurrence:
    concept_id: str
    paper_id: str
    role: ConceptRole
    definition: str | None
    page: int
    bbox: BBox | None
    verified: bool
    input_hash: str
    producer_version: str
    created_at: str
