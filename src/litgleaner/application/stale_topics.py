"""地图和综述沿当前视图选择历史，新增论文不使已发送来源失效。"""

from collections.abc import Sequence
from typing import Literal

from litgleaner.application.outline_input import producer_version as outline_producer
from litgleaner.application.outline_input import source_paper
from litgleaner.application.stale_inputs import StaleInputs
from litgleaner.application.topic_map_input import producer_version as map_producer
from litgleaner.application.topic_map_input import source_hash
from litgleaner.domain.outline import TopicOutline
from litgleaner.domain.stale import StaleItem, item
from litgleaner.domain.topic_map import TopicMap

Kind = Literal["map", "outline"]
Result = TopicMap | TopicOutline


def _sources(basis: StaleInputs, topic_id: str, kind: Kind) -> dict[str, str]:
    topic = basis.ctx.topics[topic_id]
    snapshot = basis.topic_snapshot(topic)
    if kind == "map":
        return {
            paper.id: source_hash(paper, snapshot.extractions.get(paper.id))
            for paper in snapshot.papers
        }
    return {
        paper.id: source_paper(basis.ctx, topic, paper, snapshot)[1]
        for paper in snapshot.papers
        if ((reading := snapshot.reading.get(paper.id)) is not None and reading.status == "read")
    }


def _frozen_sources(saved: Result, hashes: dict[str, str]) -> dict[str, str]:
    return {id_: hashes[id_] for id_ in saved.input_paper_ids if id_ in hashes}


def _selected(
    basis: StaleInputs,
    history: list[Result],
    hashes: dict[str, str],
    producer: str | None,
    budget: int,
) -> str | None:
    topic = basis.ctx.topics.get(history[0].topic_id)
    if topic is None:
        return None
    candidates = [
        saved
        for saved in history
        if (
            saved.topic_hash == topic.topic_hash
            and producer is not None
            and saved.producer_version == producer
            and saved.reading_language == basis.ctx.settings.reading_language
            and saved.token_budget == budget
        )
    ]
    exact = next(
        (saved for saved in candidates if _frozen_sources(saved, hashes) == saved.source_hashes),
        None,
    )
    return (exact or (candidates[0] if candidates else history[0])).id


def _check(basis: StaleInputs, records: Sequence[Result], kind: Kind) -> tuple[StaleItem, ...]:
    ctx = basis.ctx
    producer = map_producer(ctx) if kind == "map" else outline_producer(ctx)
    budget = ctx.settings.map.token_budget if kind == "map" else ctx.settings.outline.token_budget
    by_topic: dict[str, list[Result]] = {}
    for saved in sorted(records, key=lambda saved: (saved.created_at, saved.id), reverse=True):
        by_topic.setdefault(saved.topic_id, []).append(saved)
    result = []
    for topic_id, history in by_topic.items():
        hashes = _sources(basis, topic_id, kind) if topic_id in ctx.topics else {}
        selected = _selected(basis, history, hashes, producer, budget)
        for saved in history:
            reasons, unchecked = _reasons(basis, saved, hashes, producer, budget)
            result.append(
                item(
                    kind,
                    saved.id,
                    None,
                    topic_id,
                    reasons=reasons,
                    unchecked=unchecked,
                    selected=saved.id == selected,
                )
            )
    return tuple(result)


def _reasons(
    basis: StaleInputs,
    saved: Result,
    hashes: dict[str, str],
    producer: str | None,
    budget: int,
) -> tuple[list[str], list[str]]:
    reasons, unchecked = [], []
    topic = basis.ctx.topics.get(saved.topic_id)
    if topic is None:
        unchecked.append("课题配置不存在，无法核对画像与来源范围")
    else:
        if topic.topic_hash != saved.topic_hash:
            reasons.append("研究画像已变化")
        if _frozen_sources(saved, hashes) != saved.source_hashes:
            reasons.append("已发送论文的来源内容或课题、阅读范围已变化")
    if saved.reading_language != basis.ctx.settings.reading_language:
        reasons.append("阅读语言已变化")
    if saved.token_budget != budget:
        reasons.append("输入预算已变化")
    if producer is None:
        unchecked.append("未配置本任务模型，无法核对生产者")
    elif producer != saved.producer_version:
        reasons.append("模型、提示词、提供方或任务合同已变化")
    return reasons, unchecked


def check_maps(basis: StaleInputs) -> tuple[StaleItem, ...]:
    return _check(basis, basis.snapshot.maps, "map")


def check_outlines(basis: StaleInputs) -> tuple[StaleItem, ...]:
    return _check(basis, basis.snapshot.outlines, "outline")
