"""提取、评估和规则评分逐记录核对；缺生产者信息保留为未知。"""

from litgleaner.application.stages.assess import assessment_input_hash
from litgleaner.application.stages.extract import (
    extraction_input_hash,
    extraction_producer_version,
)
from litgleaner.application.stale_inputs import StaleInputs
from litgleaner.domain.stale import StaleItem, item
from litgleaner.domain.topic import Topic
from litgleaner.ports.stale import StaleAssessment
from litgleaner.ports.store import Assessment


def _input_matches(basis: StaleInputs, record: Assessment, topic: Topic) -> bool | None:
    paper, state = basis.papers.get(record.paper_id), basis.snapshot.fulltexts.get(record.paper_id)
    if paper is None or state is None:
        return None
    return record.input_hash == assessment_input_hash(
        paper,
        topic,
        state,
        basis.ctx.settings.chunker,
        reading_language=basis.ctx.settings.reading_language,
        token_budget=basis.ctx.settings.assess_token_budget,
    )


def _document_problem(basis: StaleInputs, paper_id: str) -> str | None:
    try:
        basis.document(paper_id)
    except ValueError as error:
        return str(error)
    return None


def check_extractions(basis: StaleInputs) -> tuple[StaleItem, ...]:
    ctx, result = basis.ctx, []
    producer = extraction_producer_version(ctx)
    for saved in basis.snapshot.extractions:
        reasons, unchecked = [], []
        state = basis.snapshot.fulltexts.get(saved.paper_id)
        if state is None or state.doc_key is None:
            reasons.append("当前已无可用于提取的文档")
        elif saved.input_hash != extraction_input_hash(
            state.doc_key,
            ctx.settings.chunker,
            ctx.settings.reading_language,
            ctx.settings.assess_token_budget,
        ):
            reasons.append("文档、切分、阅读语言、输入预算或提取合同已变化")
        if (
            state is not None
            and state.doc_key is not None
            and (problem := _document_problem(basis, saved.paper_id))
        ):
            unchecked.append(problem)
        if producer is None:
            unchecked.append("未配置提取模型，无法核对生产者")
        elif saved.extractor_version != producer:
            reasons.append("提取模型、提示词、提供方或输出合同已变化")
        result.append(
            item(
                "extraction",
                str(saved.id),
                saved.paper_id,
                None,
                reasons=reasons,
                unchecked=unchecked,
                selected=basis.extractions[saved.paper_id].id == saved.id,
            )
        )
    return tuple(result)


def _assessment_checks(
    basis: StaleInputs, saved: StaleAssessment
) -> tuple[list[str], list[str], bool]:
    ctx, record = basis.ctx, saved.record
    reasons: list[str] = []
    unchecked = ["旧评估未保存请求模型与提供方完整指纹，无法完整核对生产者"]
    topic = ctx.topics.get(record.topic_id)
    if topic is None:
        return reasons, [*unchecked, "课题配置不存在，无法核对画像与评价标准"], False
    rubric = ctx.rubrics[topic.rubric]
    if topic.topic_hash != record.topic_hash:
        reasons.append("研究画像已变化")
    if rubric.semantic_hash != record.semantic_hash:
        reasons.append("评价维度语义已变化")
    matches = _input_matches(basis, record, topic)
    if matches is None:
        unchecked.append("缺少当前论文或全文状态，无法核对评估输入")
    elif not matches:
        reasons.append("评估使用的摘要、全文、切分、阅读语言或输入预算已变化")
    if record.fulltext_used and (problem := _document_problem(basis, record.paper_id)):
        unchecked.append(problem)
    route = ctx.llm.routes.get("assess")
    if route is None:
        unchecked.append("未配置评估模型，无法核对提示词")
    elif saved.prompt_version != route.prompt.version:
        reasons.append("评估提示词已变化")
    latest = basis.latest_assessments(topic).get(record.paper_id)
    return reasons, unchecked, latest is not None and latest.record.id == record.id


def check_assessments(basis: StaleInputs) -> tuple[StaleItem, ...]:
    result = []
    for saved in basis.snapshot.assessments:
        reasons, unchecked, selected = _assessment_checks(basis, saved)
        record = saved.record
        result.append(
            item(
                "assessment",
                str(record.id),
                record.paper_id,
                record.topic_id,
                reasons=reasons,
                unchecked=unchecked,
                selected=selected,
            )
        )
    return tuple(result)


def check_scores(basis: StaleInputs) -> tuple[StaleItem, ...]:
    result = []
    for score in basis.snapshot.scores:
        record = score.assessment
        saved = basis.assessments[record.id]
        reasons, unchecked, selected = _assessment_checks(basis, saved)
        topic = basis.ctx.topics.get(record.topic_id)
        if topic is not None and score.policy_hash != basis.ctx.rubrics[topic.rubric].policy_hash:
            reasons.append("权重、门槛或等级线策略已变化，可显式零费用重算")
        selected = bool(
            selected
            and topic is not None
            and score.policy_hash == basis.ctx.rubrics[topic.rubric].policy_hash
            and _input_matches(basis, record, topic)
        )
        result.append(
            item(
                "score",
                f"{record.id}:{score.policy_hash}",
                record.paper_id,
                record.topic_id,
                reasons=reasons,
                unchecked=unchecked,
                selected=selected,
            )
        )
    return tuple(result)
