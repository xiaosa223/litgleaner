"""导读输入：只含这篇论文的关键概念及其掌握状态、用户写过的原话、可作类比的已掌握概念。

指纹覆盖实际发送的全部内容；知识库、原文或画像变化后指纹随之变化，但不会自动重新生成。
"""

import json
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

from litgleaner.application import learning
from litgleaner.application.context import AppContext
from litgleaner.application.documents import load_chunks, load_document
from litgleaner.application.knowledge import PaperConcept, paper_concepts
from litgleaner.application.qa_context import select_context
from litgleaner.application.topics import active_topics
from litgleaner.domain.document import NormalizedDocument
from litgleaner.domain.errors import InvalidRequest, NotFound
from litgleaner.domain.guidance import GUIDE_SCHEMA_VERSION, treatment_for
from litgleaner.domain.hashing import stable_hash
from litgleaner.domain.mastery import MasteryState
from litgleaner.domain.sections import split_sections
from litgleaner.domain.topic import Topic

TASK = "guide"
MAX_CONCEPTS = 8
MAX_KNOWN = 20
SOURCE_TOKENS = 2500
_ROLE_ORDER = {"core": 0, "prerequisite": 1, "mentioned": 2}
_EXTRACTED = ("summary", "innovation", "conclusion", "problem", "prior_gap", "approach", "method")


@dataclass(frozen=True)
class GuideInput:
    paper_id: str
    data: Mapping[str, Any]
    text: str
    fingerprint: str
    concept_ids: tuple[str, ...]
    known_ids: tuple[str, ...]
    goal_ids: tuple[str, ...]
    knowledge_empty: bool


def producer_version(ctx: AppContext) -> str | None:
    route = ctx.llm.routes.get(TASK)
    if route is None:
        return None
    return stable_hash(
        {"model": route.model, "prompt": route.prompt.version, "schema": GUIDE_SCHEMA_VERSION}
    )


def document(ctx: AppContext, paper_id: str) -> NormalizedDocument:
    if ctx.store.papers.get(paper_id) is None:
        raise NotFound("论文不存在")
    state = ctx.store.papers.get_fulltext(paper_id)
    if state is None or state.doc_key is None:
        raise InvalidRequest("这篇论文还没有解析出正文，导读要等解析完成")
    return load_document(ctx, state.doc_key)


def _topics(ctx: AppContext, paper_id: str) -> tuple[tuple[Topic, ...], str]:
    """初筛判为相关的课题优先；用户自己上传、没初筛过的论文给全部课题，但标明相关性未知。"""
    active = active_topics(ctx)
    relevant = [
        active[tid] for tid, _ in ctx.store.judgments.relevant_topics(paper_id) if tid in active
    ]
    if relevant:
        return tuple(relevant)[:2], "relevant"
    return tuple(active.values())[:2], "unknown" if active else "none"


def _extracted(ctx: AppContext, paper_id: str) -> dict[str, str]:
    extraction = ctx.store.papers.get_extraction(paper_id)
    if extraction is None:
        return {}
    result: dict[str, str] = {}
    for key in _EXTRACTED:
        value = extraction.data.get(key)
        text = value.get("text") if isinstance(value, Mapping) else value
        if isinstance(text, str) and text.strip():
            result[key] = text.strip()[:800]
    return result


def _ranked(items: tuple[PaperConcept, ...]) -> list[PaperConcept]:
    return sorted(
        items, key=lambda c: (_ROLE_ORDER.get(c.occurrence.occurrence.role, 3), c.concept.name)
    )[:MAX_CONCEPTS]


def _concepts(
    ctx: AppContext, paper_id: str, items: list[PaperConcept]
) -> tuple[list[dict[str, Any]], dict[str, MasteryState]]:
    states = learning.states(ctx, (item.concept.id for item in items), before_paper=paper_id)
    answers = learning.answers_by_concept(ctx)
    rows: list[dict[str, Any]] = []
    for item in items:
        occurrence = item.occurrence.occurrence
        level = states[item.concept.id].level
        rows.append(
            {
                "concept_id": item.concept.id,
                "name": item.concept.name,
                "role": occurrence.role,
                "level": level,
                "treatment": treatment_for(level),
                "definition": occurrence.definition if occurrence.verified else None,
                "page": occurrence.page,
                "user_words": [
                    a.text[:300] for a in answers.get(item.concept.id, ()) if a.trustworthy
                ][:2],
            }
        )
    return rows, states


def _known(ctx: AppContext, exclude: set[str]) -> list[dict[str, Any]]:
    candidates = [cid for cid in ctx.store.learning.concepts_with_events() if cid not in exclude]
    states = learning.states(ctx, candidates)
    rows: list[dict[str, Any]] = []
    for cid in sorted(candidates, key=lambda c: (-states[c].level, c)):
        concept = ctx.store.knowledge.get(cid)
        if concept is not None and states[cid].level >= 2 and len(rows) < MAX_KNOWN:
            rows.append(
                {
                    "concept_id": cid,
                    "name": concept.name,
                    "level": states[cid].level,
                    "learned_in": _source_titles(ctx, cid),  # 让模型判断是否同一领域
                }
            )
    return rows


def _source_titles(ctx: AppContext, concept_id: str) -> list[str]:
    titles: list[str] = []
    for occurrence in ctx.store.knowledge.occurrences(concept_id)[:2]:
        paper = ctx.store.papers.get(occurrence.paper_id)
        if paper is not None:
            titles.append(paper.title[:150])
    return titles


def _sections(doc: NormalizedDocument) -> list[str]:
    names = [" / ".join(section.path) for section in split_sections(doc) if section.path]
    return list(dict.fromkeys(name[:200] for name in names))[:30]


def _require_extraction(ctx: AppContext, paper_id: str) -> None:
    """导读要讲关键概念，概念来自要点提炼；没有提炼结果时先等，不生成空泛导读。"""
    if ctx.store.papers.get_extraction(paper_id) is not None:
        return
    if "extract" not in ctx.llm.routes:
        raise InvalidRequest("导读要用到“提炼要点”找出的关键概念，请先在设置里给“提炼要点”配置模型")
    raise InvalidRequest("正在提炼这篇论文的要点和关键概念，完成后就能生成导读")


def prepare(ctx: AppContext, paper_id: str) -> GuideInput:
    doc = document(ctx, paper_id)
    _require_extraction(ctx, paper_id)
    paper = ctx.store.papers.get(paper_id)
    assert paper is not None  # document() 已检查
    items = _ranked(paper_concepts(ctx, paper_id))
    concepts, _ = _concepts(ctx, paper_id, items)
    known = _known(ctx, {row["concept_id"] for row in concepts})
    topics, profile_match = _topics(ctx, paper_id)
    query = " ".join([paper.title, *(row["name"] for row in concepts), "method approach"])
    state = ctx.store.papers.get_fulltext(paper_id)
    assert state is not None and state.doc_key is not None
    sources = select_context(
        doc, load_chunks(ctx, state.doc_key), query, token_budget=SOURCE_TOKENS
    )
    data: dict[str, Any] = {
        "paper": {"title": paper.title, "abstract": (paper.abstract or "")[:1500]},
        "extraction": _extracted(ctx, paper_id),
        "concepts": concepts,
        "known_concepts": known,
        "knowledge_empty": not known and all(row["level"] <= 1 for row in concepts),
        "research_profile": "\n\n".join(topic.profile_text() for topic in topics)[:1500],
        "profile_match": profile_match,
        "goals": [{"id": g.id, "text": g.text} for topic in topics for g in topic.goals][:10],
        "sections": _sections(doc),
        "paper_sources": sources.text,
        "reading_language": ctx.settings.reading_language,
    }
    return GuideInput(
        paper_id,
        data,
        json.dumps(data, ensure_ascii=False),
        stable_hash(data),
        tuple(row["concept_id"] for row in concepts),
        tuple(row["concept_id"] for row in known),
        tuple(goal["id"] for goal in data["goals"]),
        bool(data["knowledge_empty"]),
    )
