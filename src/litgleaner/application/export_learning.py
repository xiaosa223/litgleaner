"""个人导出中的学习记录：回答卡片（含全部修订）、评价与掌握程度（第 12.8 节）。

只用导出快照里同一事务读出的数据计算，不再查库；用户写的、模型评价的、作者原文分开标明。
"""

from collections.abc import Mapping
from dataclasses import dataclass

from litgleaner.application.export_format import label, literal, page_label
from litgleaner.domain.guidance import AnswerCard, Evaluation, Guide
from litgleaner.domain.mastery import DEFAULT_POLICY, MasteryEvent, compute_mastery
from litgleaner.ports.export import ExportSnapshot

LEVELS = ("没见过", "见过", "能解释", "能应用")
_VERDICTS = {"correct": "对", "missing": "漏了", "misunderstood": "理解偏了"}
_OVERALL = {"correct": "答得对", "partial": "部分正确", "incorrect": "需要再想想"}


@dataclass(frozen=True)
class LearningExport:
    paper_sections: Mapping[str, str]  # 论文 ID → “用户回答卡片”一节
    concept_sections: Mapping[str, str]  # 概念 ID → “掌握程度与回答”一节
    answer_cards: int


def _evaluation(evaluation: Evaluation, names: Mapping[str, str]) -> list[str]:
    lines = [f"**模型评价**（{evaluation.model}）：{_OVERALL[evaluation.overall]}"]
    for verdict in evaluation.verdicts:
        lines.append(
            f"{label(names.get(verdict.concept_id, verdict.concept_id))}："
            f"{_VERDICTS[verdict.verdict]}。{label(verdict.comment)}"
        )
        for quote in verdict.citations:
            lines += [f"作者原文（已核验，{page_label(quote.page)}）", literal(quote.quote)]
    lines.append(f"**模型参考答案**：{label(evaluation.reference.text)}")
    return lines


def _paper_section(
    guide: Guide, cards: list[AnswerCard], evaluations: Mapping[str, Evaluation]
) -> str:
    names = {c.concept_id: c.name for c in guide.concepts}
    parts: list[str] = []
    for question in guide.questions:
        revisions = [c for c in cards if c.question_index == question.index]
        if not revisions:
            continue
        parts.append(f"### 引导问题（模型生成）：{label(question.text)}")
        for card in revisions:
            parts += [
                f"**用户回答 · 第 {card.revision} 版**（{card.created_at}）",
                literal(card.text),
            ]
            if evaluation := evaluations.get(card.id):
                parts += _evaluation(evaluation, names)
    return "\n\n".join(parts)


def _seen(snapshot: ExportSnapshot) -> frozenset[str]:
    opened = {item.paper.id for item in snapshot.papers if item.reading is not None}
    return frozenset(
        concept.concept.id
        for concept in snapshot.concepts
        if any(o.paper_id in opened for o in concept.occurrences)
    )


Answered = tuple[AnswerCard, str, str | None]  # (卡片, 问题, 对该概念的评价)


def _answers(
    snapshot: ExportSnapshot, guides: Mapping[str, Guide], evaluations: Mapping[str, Evaluation]
) -> dict[str, list[Answered]]:
    """每个概念下各问题最新修订的回答。"""
    latest: dict[tuple[str, int], AnswerCard] = {}
    for card in snapshot.learning.cards:
        latest[(card.explanation_id, card.question_index)] = card  # 已按修订号升序
    grouped: dict[str, list[Answered]] = {}
    for card in latest.values():
        guide = guides.get(card.explanation_id)
        if guide is None:
            continue
        question = guide.questions[card.question_index]
        evaluation = evaluations.get(card.id)
        verdicts = {v.concept_id: v.verdict for v in evaluation.verdicts} if evaluation else {}
        for cid in question.concept_ids:
            grouped.setdefault(cid, []).append((card, question.text, verdicts.get(cid)))
    return grouped


def _concepts(
    snapshot: ExportSnapshot, guides: Mapping[str, Guide], evaluations: Mapping[str, Evaluation]
) -> dict[str, str]:
    answers = _answers(snapshot, guides, evaluations)
    events: dict[str, list[MasteryEvent]] = {}
    for event in snapshot.learning.events:
        events.setdefault(event.concept_id, []).append(event)
    seen = _seen(snapshot)
    sections: dict[str, str] = {}
    for cid in set(answers) | set(events):
        state = compute_mastery(events.get(cid, ()), seen=cid in seen, policy=DEFAULT_POLICY)
        source = "系统按规则算出，用户改过" if state.overridden else "系统按规则算出"
        lines = [f"掌握程度（{source}）：{LEVELS[state.level]}"]
        if state.due_at:
            lines.append(f"下次复习：{state.due_at}")
        for card, question, verdict in answers.get(cid, ()):
            note = f"（评价：{_VERDICTS[verdict]}）" if verdict else ""
            lines += [f"用户回答{note}，问题“{label(question)}”", literal(card.text)]
        sections[cid] = "\n\n".join(lines)
    return sections


def _corrections(snapshot: ExportSnapshot) -> dict[str, list[str]]:
    latest: dict[str, dict[str, str]] = {}
    for item in snapshot.learning.corrections:  # 时间升序，后写的覆盖
        latest.setdefault(item.paper_id, {})[item.field] = item.value
    sections: dict[str, list[str]] = {}
    for pid, fields in latest.items():
        lines = [f"- {name}：{label(value)}" for name, value in fields.items() if value]
        if lines:
            sections[pid] = ["### 用户修正的元数据（覆盖显示，来源元数据见上）", "\n".join(lines)]
    return sections


def collect(snapshot: ExportSnapshot) -> LearningExport:
    guides = {guide.id: guide for guide in snapshot.learning.guides}
    evaluations = {item.card_id: item for item in snapshot.learning.evaluations}
    papers: dict[str, list[str]] = _corrections(snapshot)
    for guide in guides.values():
        cards = [c for c in snapshot.learning.cards if c.explanation_id == guide.id]
        if section := _paper_section(guide, cards, evaluations):
            papers.setdefault(guide.paper_id, []).append(section)
    return LearningExport(
        {pid: "\n\n".join(parts) for pid, parts in papers.items()},
        _concepts(snapshot, guides, evaluations),
        len(snapshot.learning.cards),
    )
