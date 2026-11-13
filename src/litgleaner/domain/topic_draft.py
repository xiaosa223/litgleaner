"""检索式草稿（第 7.2 节）：模型起草、研究者确认后才写入主题文件。"""

from collections.abc import Iterable, Mapping, Sequence
from typing import Annotated, Any

from pydantic import Field, StringConstraints

from litgleaner.domain.base import FrozenModel
from litgleaner.domain.glossary import GlossaryTerm
from litgleaner.domain.hashing import stable_hash
from litgleaner.domain.topic import Topic, TopicQuery

Term = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=120)]
Reason = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=600)]
MAX_INCLUDE = 20
MAX_SYNONYMS = 8
MAX_EXCLUDE = 20
MAX_GLOSSARY = 60


class DraftTerm(FrozenModel):
    term: Term
    reason: Reason
    synonyms: tuple[Term, ...] = Field(default=(), max_length=MAX_SYNONYMS)


class DraftExclusion(FrozenModel):
    term: Term
    reason: Reason


class TopicDraft(FrozenModel):
    topic_id: str
    plan_hash: str
    profile_hash: str
    model: str
    created_at: str
    reading_language: str
    include: tuple[DraftTerm, ...]
    exclude: tuple[DraftExclusion, ...] = ()
    glossary: tuple[GlossaryTerm, ...] = ()
    seed_titles: tuple[str, ...] = ()
    warnings: tuple[str, ...] = ()


def profile_hash(topic: Topic) -> str:
    """草稿只依据研究者写的背景、内容和目标；确认写入检索式后草稿仍然对应同一份画像。"""
    return stable_hash(
        {
            "background": topic.background,
            "content": topic.content,
            "goals": [goal.model_dump() for goal in topic.goals],
        }
    )


def _key(text: str) -> str:
    return " ".join(text.split()).casefold()


def _unique(values: Iterable[str], taken: set[str]) -> list[str]:
    kept = []
    for value in values:
        if _key(value) and _key(value) not in taken:
            taken.add(_key(value))
            kept.append(" ".join(value.split()))
    return kept


def build_query(include: Sequence[tuple[str, Sequence[str]]], exclude: Sequence[str]) -> TopicQuery:
    """去重（不分大小写）；排除词与检索词重叠时报错，免得一个词同时检索又排除。"""
    taken: set[str] = set()
    terms: list[str] = []
    synonyms: dict[str, tuple[str, ...]] = {}
    for term, variants in include:
        kept = _unique([term], taken)
        if not kept:
            continue
        terms.append(kept[0])
        if extra := _unique(variants, taken):
            synonyms[kept[0]] = tuple(extra)
    excluded = _unique(exclude, set())
    if not terms:
        raise ValueError("至少需要一个检索词")
    if any(_key(word) in taken for word in excluded):
        raise ValueError("排除词不能同时是检索词或同义词")
    return TopicQuery(include=tuple(terms), synonyms=synonyms, exclude=tuple(excluded))


def draft_from_answer(data: Mapping[str, Any], **fields: Any) -> TopicDraft:
    """模型输出已过 JSON Schema；这里再按规则清理，空白和重复的条目丢掉并记警告。"""
    warnings = list(fields.pop("warnings", ()))
    seen: set[str] = set()
    include = []
    for item in data["include"]:
        if _unique([item["term"]], seen):
            include.append(DraftTerm(**{**item, "synonyms": _unique(item["synonyms"], seen)}))
    exclude = [DraftExclusion(**item) for item in data["exclude"] if _key(item["term"]) not in seen]
    if len(exclude) < len(data["exclude"]):
        warnings.append("有排除词和检索词重复，已去掉")
    glossary = tuple(
        GlossaryTerm(
            forms=(item["term"],), translation=item["translation"], definition=item["definition"]
        )
        for item in data["glossary"]
    )
    return TopicDraft(
        include=tuple(include), exclude=tuple(exclude), glossary=glossary,
        warnings=tuple(warnings), **fields,
    )  # fmt: skip
