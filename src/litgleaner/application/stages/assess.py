"""评估阶段：LLM 只给各维度等级和证据。指标变化后不会自动重评，避免意外费用。"""

import json
from collections.abc import Iterable, Mapping
from typing import Any

from litgleaner.application.context import AppContext
from litgleaner.application.documents import full_text_of, load_chunks, paper_text
from litgleaner.application.stages.base import Stage
from litgleaner.application.topics import active_topics
from litgleaner.domain.chunker import ChunkedDocument, ChunkerConfig
from litgleaner.domain.evidence import verify_quote
from litgleaner.domain.hashing import stable_hash
from litgleaner.domain.paper import Paper
from litgleaner.domain.rubric import Rubric, apply_fulltext_constraint
from litgleaner.domain.topic import Topic
from litgleaner.ports.store import AssessmentKey, FullTextState, JobSpec, NewAssessment

_ABSTRACT_ONLY = frozenset({"unavailable", "failed"})


def _uses_fulltext(state: FullTextState) -> bool:
    return state.status == "parsed" and state.doc_key is not None


def _is_ready(ctx: AppContext, paper_id: str) -> FullTextState | None:
    state = ctx.store.papers.get_fulltext(paper_id)
    if state is None:
        return None
    if state.status in _ABSTRACT_ONLY:
        return state
    has_extraction = ctx.store.papers.get_extraction(paper_id) is not None
    return state if _uses_fulltext(state) and has_extraction else None


def assessment_input_hash(
    paper: Paper, topic: Topic, state: FullTextState, chunker: ChunkerConfig
) -> str:
    """评估输入的指纹。用全文时，切分规则决定送给模型的内容，所以计入切分版本。"""
    fulltext = _uses_fulltext(state)
    return stable_hash(
        {
            "topic": topic.topic_hash,
            "abstract": paper.abstract,
            "doc": state.doc_key if fulltext else None,
            "chunker": chunker.version if fulltext else None,
        }
    )


def assess_job(paper_id: str, topic: Topic, rubric: Rubric, input_hash: str) -> JobSpec:
    return JobSpec(
        key=f"assess:{paper_id}:{topic.id}:{rubric.semantic_hash}:{input_hash}",
        stage="assess",
        payload={"paper_id": paper_id, "topic_id": topic.id},
    )


def _needs_assessment(
    keys: tuple[AssessmentKey, ...], semantic: str, topic_hash: str, fulltext_now: bool
) -> bool:
    """只有两种情况自动评估：首次评估，和全文到位后从摘要升级。

    其他输入变化（切分参数、摘要被正式版更新等）只改变指纹，由用户显式 reassess，
    不能静默产生费用。旧标准下已有评估的论文同样要显式 reassess。
    """
    current = [k for k in keys if k.semantic_hash == semantic and k.topic_hash == topic_hash]
    if current:
        return fulltext_now and not any(k.fulltext_used for k in current)
    judged_under_old_standard = bool(keys) and not any(k.semantic_hash == semantic for k in keys)
    return not judged_under_old_standard


def _dimension_prompt(rubric: Rubric) -> str:
    return json.dumps(
        [
            {
                "key": dim.key,
                "label": dim.label,
                "question": dim.question,
                "anchors": dict(dim.anchors),
                "requires_fulltext": dim.requires_fulltext,
            }
            for dim in rubric.dimensions
        ],
        ensure_ascii=False,
        indent=2,
    )


def _evidence(dimensions: Mapping[str, Any], source: str) -> dict[str, Any]:
    return {
        key: {
            "reason": item["reason"],
            "quote": item["quote"],
            "verified": verify_quote(item["quote"], source),
        }
        for key, item in dimensions.items()
    }


class AssessStage(Stage):
    name = "assess"

    def plan(self, ctx: AppContext) -> Iterable[JobSpec]:
        for topic in active_topics(ctx).values():
            rubric = ctx.rubrics[topic.rubric]
            for paper_id in ctx.store.judgments.relevant_papers(topic.id, topic.topic_hash):
                job = self._plan_one(ctx, paper_id, topic, rubric)
                if job:
                    yield job

    def _plan_one(
        self, ctx: AppContext, paper_id: str, topic: Topic, rubric: Rubric
    ) -> JobSpec | None:
        state = _is_ready(ctx, paper_id)
        paper = ctx.store.papers.get(paper_id)
        if state is None or paper is None:
            return None
        keys = ctx.store.judgments.assessment_keys(paper_id, topic.id)
        fulltext_now = _uses_fulltext(state)
        if not _needs_assessment(keys, rubric.semantic_hash, topic.topic_hash, fulltext_now):
            return None
        input_hash = assessment_input_hash(paper, topic, state, ctx.settings.chunker)
        return assess_job(paper_id, topic, rubric, input_hash)

    def process(self, ctx: AppContext, payload: Mapping[str, Any]) -> None:
        topic = ctx.topics[payload["topic_id"]]
        rubric = ctx.rubrics[topic.rubric]
        paper = ctx.store.papers.get(payload["paper_id"])
        state = ctx.store.papers.get_fulltext(payload["paper_id"])
        if paper is None or state is None:
            raise ValueError(f"论文未就绪：{payload['paper_id']}")
        fulltext_used = _uses_fulltext(state)
        chunked: ChunkedDocument | None = (
            load_chunks(ctx, state.doc_key) if state.doc_key and fulltext_used else None
        )
        result = ctx.llm.call(
            "assess",
            {
                "topic_profile": topic.profile_text(),
                "rubric_dimensions": _dimension_prompt(rubric),
            },
            paper_text(ctx, paper, chunked),
            rubric.assessment_schema(),
        )
        dimensions = result.data["dimensions"]
        source = full_text_of(chunked) if chunked else (paper.abstract or "")
        raw_levels = {key: item["level"] for key, item in dimensions.items()}
        ctx.store.judgments.insert_assessment(
            NewAssessment(
                paper_id=paper.id,
                topic_id=topic.id,
                topic_hash=topic.topic_hash,
                semantic_hash=rubric.semantic_hash,
                input_hash=assessment_input_hash(paper, topic, state, ctx.settings.chunker),
                levels=apply_fulltext_constraint(rubric, raw_levels, fulltext_used),
                evidence=_evidence(dimensions, source),
                fulltext_used=fulltext_used,
                model=result.model,
                prompt_version=result.prompt_version,
            ),
            ctx.now_iso(),
        )
