"""掌握程度、用户改等级、复习自评与到期列表。全部不调用模型（第 12.7 节）。"""

from collections.abc import Iterable, Mapping
from dataclasses import dataclass

from ulid import ULID

from litgleaner.application import concept_links, corrections, knowledge
from litgleaner.application.context import AppContext
from litgleaner.domain.errors import InvalidRequest, NotFound
from litgleaner.domain.guidance import AnswerCard, Guide
from litgleaner.domain.mastery import (
    DEFAULT_POLICY,
    MasteryEvent,
    MasteryState,
    Rating,
    compute_mastery,
    is_due,
)

RATINGS: tuple[Rating, ...] = ("remembered", "fuzzy", "forgot")
_REVIEW_SCAN = 2000


@dataclass(frozen=True)
class UserAnswer:
    """用户自己写的话：只来自回答卡片的最新修订。"""

    card_id: str
    paper_id: str
    paper_title: str
    question: str
    text: str
    created_at: str
    verdict: str | None = None  # 评价对这个概念的判断；None 表示还没评价
    overall: str | None = None  # 整题评价：概念讲对但整题有误时，原话里可能夹着别的错误

    @property
    def trustworthy(self) -> bool:
        """可以作为锚点引用给读者：这个概念讲对了，整题也没有错误（或还没评价）。"""
        return self.verdict in {"correct", None} and self.overall in {"correct", None}


_VERDICT_ORDER = {"correct": 0, None: 1, "missing": 2, "misunderstood": 3}
_OVERALL_ORDER = {"correct": 0, None: 1, "partial": 2, "incorrect": 3}


@dataclass(frozen=True)
class ConceptCard:
    summary: knowledge.ConceptSummary
    mastery: MasteryState
    due: bool
    answers: tuple[UserAnswer, ...]
    related: tuple[concept_links.RelatedView, ...] = ()
    merged: tuple[concept_links.MergedName, ...] = ()  # 用户确认并入这个概念的其他写法


@dataclass(frozen=True)
class ConceptCards:
    items: tuple[ConceptCard, ...]
    total: int
    due: int
    limit: int
    offset: int


def states(
    ctx: AppContext, concept_ids: Iterable[str], *, before_paper: str | None = None
) -> dict[str, MasteryState]:
    """before_paper：给这篇论文写导读时，只把其他论文算作“见过”，正在读的这篇不算。"""
    ids = tuple(dict.fromkeys(concept_ids))
    members = concept_links.groups(ctx, ids)  # 确认合并后的概念一起算
    every = {m for group in members.values() for m in group}
    events = ctx.store.learning.events(every)
    seen = ctx.store.learning.seen(every, before_paper)
    return {
        cid: compute_mastery(
            [e for m in members[cid] for e in events.get(m, ())],
            seen=any(m in seen for m in members[cid]),
            policy=DEFAULT_POLICY,
        )
        for cid in ids
    }


def answers_by_concept(ctx: AppContext) -> dict[str, tuple[UserAnswer, ...]]:
    """每个概念下用户写过的回答（各问题的最新修订）。评价为“对”的在前，理解偏了的在后并标明，
    复习时不把错误理解当作参照。
    """
    mapping = concept_links.canonical(ctx)
    cards = ctx.store.learning.latest_cards(_REVIEW_SCAN)
    guides = ctx.store.learning.guides(card.explanation_id for card in cards)
    evaluations = ctx.store.learning.evaluations(card.id for card in cards)
    titles = corrections.titles(ctx, (card.paper_id for card in cards))
    grouped: dict[str, list[UserAnswer]] = {}
    for card in cards:
        guide = guides.get(card.explanation_id)
        if guide is None:
            continue
        question = guide.questions[card.question_index]
        evaluation = evaluations.get(card.id)
        verdicts = {v.concept_id: v.verdict for v in evaluation.verdicts} if evaluation else {}
        for concept_id in question.concept_ids:
            item = _answer(
                card,
                titles[card.paper_id],
                question.text,
                verdicts.get(concept_id),
                evaluation.overall if evaluation else None,
            )
            grouped.setdefault(mapping.get(concept_id, concept_id), []).append(item)
    return {
        key: tuple(
            sorted(
                value,
                key=lambda a: (
                    _VERDICT_ORDER.get(a.verdict, 1),
                    _OVERALL_ORDER.get(a.overall, 1),
                ),
            )
        )
        for key, value in grouped.items()
    }


def _answer(
    card: AnswerCard, title: str, question: str, verdict: str | None, overall: str | None
) -> UserAnswer:
    return UserAnswer(
        card.id, card.paper_id, title, question, card.text, card.created_at, verdict, overall
    )


def concept_cards(
    ctx: AppContext,
    *,
    topic_id: str | None = None,
    query: str = "",
    limit: int = 200,
    offset: int = 0,
) -> ConceptCards:
    page = knowledge.concepts(ctx, topic_id=topic_id, query=query, limit=limit, offset=offset)
    mapping = concept_links.canonical(ctx)
    kept = [item for item in page.items if item.concept.id not in mapping]  # 已并入别的概念的不单列
    ids = [item.concept.id for item in kept]
    mastery = states(ctx, ids)
    answers = answers_by_concept(ctx)
    now = ctx.now_iso()
    items = tuple(
        ConceptCard(
            item,
            mastery[item.concept.id],
            is_due(mastery[item.concept.id], now),
            answers.get(item.concept.id, ())[:3],
            concept_links.related(ctx, item.concept.id),
            concept_links.merged_into(ctx, item.concept.id),
        )
        for item in kept
    )
    hidden = len(page.items) - len(kept)
    return ConceptCards(items, page.total - hidden, len(due_ids(ctx)), page.limit, page.offset)


def _require(ctx: AppContext, concept_id: str) -> None:
    if ctx.store.knowledge.get(concept_id) is None:
        raise NotFound("概念不存在")


def _record(ctx: AppContext, event: MasteryEvent) -> MasteryState:
    ctx.store.learning.add_event(event)
    return states(ctx, (event.concept_id,))[event.concept_id]


def set_level(ctx: AppContext, concept_id: str, level: int) -> MasteryState:
    """用户直接改等级：优先于之前的系统评价，本身也记为一条事件。"""
    _require(ctx, concept_id)
    if type(level) is not int or not 0 <= level <= 3:
        raise InvalidRequest("掌握程度只能是 0–3")
    event = MasteryEvent(str(ULID()), concept_id, "override", level, None, None, ctx.now_iso())
    return _record(ctx, event)


def review(ctx: AppContext, concept_id: str, rating: str) -> MasteryState:
    """复习自评，不调用模型。只有进入复习计划（能解释及以上）的概念才能自评。"""
    _require(ctx, concept_id)
    if rating not in RATINGS:
        raise InvalidRequest("自评只能是 remembered、fuzzy 或 forgot")
    if not is_due(states(ctx, (concept_id,))[concept_id], ctx.now_iso()):
        raise InvalidRequest("这个概念还没到复习时间")
    rated = RATINGS[RATINGS.index(rating)]
    event = MasteryEvent(str(ULID()), concept_id, "review", None, rated, None, ctx.now_iso())
    return _record(ctx, event)


def due_ids(ctx: AppContext) -> tuple[str, ...]:
    """到期的概念，最早到期的在前。只有掌握事件的概念才可能在复习计划里。"""
    now = ctx.now_iso()
    mapping = concept_links.canonical(ctx)
    roots = {mapping.get(cid, cid) for cid in ctx.store.learning.concepts_with_events()}
    current = states(ctx, roots)
    due = [(state.due_at or "", cid) for cid, state in current.items() if is_due(state, now)]
    return tuple(cid for _, cid in sorted(due))


def due_cards(ctx: AppContext, limit: int = 20) -> tuple[ConceptCard, ...]:
    ids = due_ids(ctx)[: max(1, min(limit, 100))]
    mastery = states(ctx, ids)
    answers = answers_by_concept(ctx)
    cards: list[ConceptCard] = []
    for cid in ids:
        concept = ctx.store.knowledge.get(cid)
        if concept is None:
            continue
        cards.append(
            ConceptCard(
                knowledge.summary(ctx, concept), mastery[cid], True, answers.get(cid, ())[:3]
            )
        )
    return tuple(cards)


def guide_concept_states(ctx: AppContext, guide: Guide) -> Mapping[str, MasteryState]:
    return states(ctx, (item.concept_id for item in guide.concepts))
