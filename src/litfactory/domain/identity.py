"""去重规则：标识符相同才自动合并；标题加第一作者相同视为高置信；其余不猜。"""

import re
import unicodedata
from dataclasses import dataclass
from typing import Literal

from litfactory.domain.paper import SourceRecord

_DOI_PREFIX = re.compile(r"^(?:https?://(?:dx\.)?doi\.org/|doi:)", re.IGNORECASE)
_DOI_BODY = re.compile(r"^10\.\d{1,9}/\S+$")
_ARXIV_PREFIX = re.compile(r"^(?:https?://arxiv\.org/(?:abs|pdf)/|arxiv:)", re.IGNORECASE)
_ARXIV_BODY = re.compile(r"^(\d{4}\.\d{4,5}|[a-z\-]+(?:\.[A-Z]{2})?/\d{7})(?:v\d+)?(?:\.pdf)?$")


# “Editorial”“Erratum” 这类短标题到处都有，只靠标题不能判断是同一篇。
MIN_TITLE_KEY_LENGTH = 15


def normalize_doi(value: str | None) -> str | None:
    if not value:
        return None
    candidate = _DOI_PREFIX.sub("", value.strip()).lower()
    return candidate if _DOI_BODY.match(candidate) else None


def normalize_arxiv_id(value: str | None) -> str | None:
    if not value:
        return None
    match = _ARXIV_BODY.match(_ARXIV_PREFIX.sub("", value.strip()))
    return match.group(1) if match else None


def title_key(title: str) -> str:
    """大小写、标点、全半角和空白都不影响匹配，非拉丁文字保留。"""
    normalized = unicodedata.normalize("NFKC", title).casefold()
    return "".join(ch for ch in normalized if ch.isalnum())


def _name_parts(author: str) -> tuple[str, ...]:
    """统一成“名 姓”的顺序。“Smith, Alice” 和 “Alice Smith” 是同一个人。"""
    family, comma, given = author.partition(",")
    ordered = f"{given} {family}" if comma else author
    return tuple(part.strip(".").casefold() for part in ordered.split())


def _names_match(first: str, second: str) -> bool:
    """姓相同还不够。要求名字能对上，或者首字母一致（A. Smith 和 Alice Smith）。"""
    left, right = _name_parts(first), _name_parts(second)
    if len(left) < 2 or len(right) < 2:
        return False
    if set(left) == set(right):  # 中文姓名常有 “Wei Li” 和 “Li Wei” 两种写法
        return True
    return left[-1] == right[-1] and left[0][0] == right[0][0]


@dataclass(frozen=True)
class Alias:
    scheme: str
    value: str


@dataclass(frozen=True)
class ExistingPaper:
    id: str
    authors: tuple[str, ...]


@dataclass(frozen=True)
class Resolution:
    kind: Literal["new", "existing", "conflict"]
    paper_id: str | None = None
    reason: str | None = None
    conflicting_ids: tuple[str, ...] = ()


def record_aliases(record: SourceRecord) -> tuple[Alias, ...]:
    candidates = (
        ("doi", normalize_doi(record.doi)),
        ("arxiv", normalize_arxiv_id(record.arxiv_id)),
        *sorted(record.external_ids.items()),
    )
    return tuple(Alias(scheme, value) for scheme, value in candidates if value)


def decide_resolution(
    record: SourceRecord,
    alias_hits: frozenset[str],
    title_matches: tuple[ExistingPaper, ...],
) -> Resolution:
    if len(alias_hits) > 1:
        return Resolution("conflict", conflicting_ids=tuple(sorted(alias_hits)))
    if alias_hits:
        return Resolution("existing", paper_id=next(iter(alias_hits)), reason="alias")
    return _resolve_by_title(record, title_matches)


def _resolve_by_title(record: SourceRecord, matches: tuple[ExistingPaper, ...]) -> Resolution:
    if not matches:
        return Resolution("new")
    if record.authors and len(title_key(record.title)) >= MIN_TITLE_KEY_LENGTH:
        for existing in matches:
            if existing.authors and _names_match(record.authors[0], existing.authors[0]):
                return Resolution("existing", paper_id=existing.id, reason="title_author")
    return Resolution("new", conflicting_ids=tuple(sorted(item.id for item in matches)))
