"""评估阶段：LLM 只给各维度等级和证据。指标变化后不会自动重评，避免意外费用。"""

import json
from collections.abc import Iterable, Mapping
from typing import Any

from litgleaner.application.context import AppContext
from litgleaner.application.documents import load_chunks, load_document, paper_header
from litgleaner.application.goal_links import verified_goal_links
from litgleaner.application.knowledge_ingest import ExtractionSource, extraction_source
from litgleaner.application.llm_gateway import LLMResult
from litgleaner.application.stages.base import Stage
from litgleaner.application.topics import active_topics
from litgleaner.domain.chunker import ChunkerConfig
from litgleaner.domain.evidence import clean_untrusted, verify_quote
from litgleaner.domain.hashing import stable_hash
from litgleaner.domain.language import language_name
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
    paper: Paper,
    topic: Topic,
    state: FullTextState,
    chunker: ChunkerConfig,
    *,
    reading_language: str = "zh-CN",
    token_budget: int = 6000,
) -> str:
    """评估输入的指纹。用全文时，切分规则决定送给模型的内容，所以计入切分版本。"""
    fulltext = _uses_fulltext(state)
    return stable_hash(
        {
            "topic": topic.topic_hash,
            "abstract": paper.abstract,
            "doc": state.doc_key if fulltext else None,
            "chunker": chunker.version if fulltext else None,
            "reading_language": reading_language,
            "source_version": "page-blocks-v1" if fulltext else "metadata-v1",
            "token_budget": token_budget if fulltext else None,
        }
    )


def assess_job(paper_id: str, topic: Topic, rubric: Rubric, input_hash: str) -> JobSpec:
    return JobSpec(
        key=f"assess:{paper_id}:{topic.id}:{rubric.semantic_hash}:{input_hash}",
        stage="assess",
        payload={
            "paper_id": paper_id,
            "topic_id": topic.id,
            "input_hash": input_hash,
            "semantic_hash": rubric.semantic_hash,
        },
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


def _evidence(dimensions: Mapping[str, Any], source: str, sent: str) -> dict[str, Any]:
    return {
        key: {
            "reason": item["reason"],
            "quote": item["quote"],
            "verified": verify_quote(item["quote"], source) and verify_quote(item["quote"], sent),
        }
        for key, item in dimensions.items()
    }


def _assessment_result(
    ctx: AppContext,
    paper: Paper,
    topic: Topic,
    state: FullTextState,
    *,
    input_hash: str,
    result: LLMResult,
    source: ExtractionSource,
) -> NewAssessment:
    fulltext_used = _uses_fulltext(state)
    document = load_document(ctx, state.doc_key) if fulltext_used and state.doc_key else None
    original = (
        "\n".join(text for block in document.blocks for text in (block.caption, block.text) if text)
        if document
        else paper.abstract or ""
    )
    dimensions = result.data["dimensions"]
    rubric = ctx.rubrics[topic.rubric]
    raw_levels = {key: item["level"] for key, item in dimensions.items()}
    return NewAssessment(
        paper_id=paper.id,
        topic_id=topic.id,
        topic_hash=topic.topic_hash,
        semantic_hash=rubric.semantic_hash,
        input_hash=input_hash,
        levels=apply_fulltext_constraint(rubric, raw_levels, fulltext_used),
        evidence=_evidence(dimensions, original, clean_untrusted(source.text)),
        fulltext_used=fulltext_used,
        model=result.model,
        prompt_version=result.prompt_version,
        goal_links=verified_goal_links(
            result.data["goal_links"],
            topic,
            source.text,
            document=document,
            abstract=paper.abstract,
            sent_pages={
                page: "\n".join(sent.text for sent in source.sources if sent.page == page)
                for page in {sent.page for sent in source.sources}
            },
        ),
    )


def _assessment_source(ctx: AppContext, paper: Paper, state: FullTextState) -> ExtractionSource:
    if _uses_fulltext(state) and state.doc_key:
        return extraction_source(
            paper,
            load_document(ctx, state.doc_key),
            load_chunks(ctx, state.doc_key),
            ctx.settings.assess_token_budget,
        )
    return ExtractionSource(paper_header(paper), ())


def _skip_processing(
    ctx: AppContext,
    paper_id: str,
    topic: Topic,
    rubric: Rubric,
    input_hash: str,
    *,
    payload: Mapping[str, Any],
) -> bool:
    if (
        payload.get("input_hash", input_hash) != input_hash
        or payload.get("semantic_hash", rubric.semantic_hash) != rubric.semantic_hash
    ):
        return True
    keys = ctx.store.judgments.assessment_keys(paper_id, topic.id)
    if "input_hash" not in payload:
        return bool(keys)  # 无计划指纹的旧任务只允许首次评估，不自动重评历史
    return any(
        key.input_hash == input_hash
        and key.semantic_hash == rubric.semantic_hash
        and key.topic_hash == topic.topic_hash
        for key in keys
    )


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
        input_hash = assessment_input_hash(
            paper,
            topic,
            state,
            ctx.settings.chunker,
            reading_language=ctx.settings.reading_language,
            token_budget=ctx.settings.assess_token_budget,
        )
        return assess_job(paper_id, topic, rubric, input_hash)

    def process(self, ctx: AppContext, payload: Mapping[str, Any]) -> None:
        topic = ctx.topics[payload["topic_id"]]
        rubric = ctx.rubrics[topic.rubric]
        paper = ctx.store.papers.get(payload["paper_id"])
        state = ctx.store.papers.get_fulltext(payload["paper_id"])
        if paper is None or state is None:
            raise ValueError(f"论文未就绪：{payload['paper_id']}")
        input_hash = assessment_input_hash(
            paper,
            topic,
            state,
            ctx.settings.chunker,
            reading_language=ctx.settings.reading_language,
            token_budget=ctx.settings.assess_token_budget,
        )
        if _skip_processing(ctx, paper.id, topic, rubric, input_hash, payload=payload):
            return
        source = _assessment_source(ctx, paper, state)
        result = ctx.llm.call(
            "assess",
            {
                "topic_profile": topic.profile_text(),
                "rubric_dimensions": _dimension_prompt(rubric),
                "output_language": language_name(ctx.settings.reading_language),
            },
            source.text,
            rubric.assessment_schema(),
            paper_id=paper.id,
        )
        ctx.store.judgments.insert_assessment(
            _assessment_result(
                ctx, paper, topic, state, input_hash=input_hash, result=result, source=source
            ),
            ctx.now_iso(),
        )
