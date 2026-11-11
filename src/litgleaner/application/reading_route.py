"""从当前画像的初筛、评分和引文事实生成零模型费用的阅读路线。"""

from dataclasses import dataclass

from litgleaner.application.context import AppContext
from litgleaner.application.stages.assess import assessment_input_hash
from litgleaner.domain.errors import NotFound
from litgleaner.domain.goal_links import GoalLink
from litgleaner.domain.paper import Paper
from litgleaner.domain.reading_route import RouteCandidate, RouteKind, order_route
from litgleaner.domain.topic import Topic
from litgleaner.ports.store import FullTextState, ScoredAssessment


@dataclass(frozen=True)
class RoutePaper:
    paper_id: str
    title: str
    published_at: str | None
    publication_types: tuple[str, ...]
    topic_citations: int
    reading_value: int | None
    provisional: bool | None
    goal_links: tuple[GoalLink, ...]


@dataclass(frozen=True)
class ReadingGroup:
    kind: RouteKind
    papers: tuple[RoutePaper, ...]


@dataclass(frozen=True)
class ReadingRoute:
    topic_id: str
    topic_hash: str
    groups: tuple[ReadingGroup, ...]
    ungrouped: tuple[RoutePaper, ...]
    total: int


def _score(
    ctx: AppContext,
    topic: Topic,
    paper: Paper,
    state: FullTextState | None,
    latest: ScoredAssessment | None,
) -> ScoredAssessment | None:
    if latest is None or state is None:
        return None
    expected = assessment_input_hash(
        paper,
        topic,
        state,
        ctx.settings.chunker,
        reading_language=ctx.settings.reading_language,
        token_budget=ctx.settings.assess_token_budget,
    )
    return latest if latest.assessment.input_hash == expected else None


def reading_route(ctx: AppContext, topic_id: str, *, limit_per_group: int = 10) -> ReadingRoute:
    topic = ctx.topics.get(topic_id)
    if topic is None:
        raise NotFound(f"课题不存在：{topic_id}")
    rubric = ctx.rubrics[topic.rubric]
    snapshot = ctx.store.reading_routes.snapshot(
        topic.id, topic.topic_hash, rubric.semantic_hash, rubric.policy_hash
    )
    candidates: list[RouteCandidate] = []
    views: dict[str, RoutePaper] = {}
    for paper in snapshot.papers:
        paper_id = paper.id
        citations = snapshot.citations.get(paper_id, 0)
        score = _score(
            ctx, topic, paper, snapshot.fulltexts.get(paper_id), snapshot.scores.get(paper_id)
        )
        value = score.score if score else None
        candidates.append(RouteCandidate(paper, citations, value))
        views[paper_id] = RoutePaper(
            paper_id,
            paper.title,
            paper.published_at,
            paper.publication_types,
            citations,
            value,
            score.provisional if score else None,
            score.assessment.goal_links if score else (),
        )
    today = ctx.clock().astimezone(ctx.settings.timezone).date()
    order = order_route(tuple(candidates), today, limit_per_group=limit_per_group)
    groups = tuple(
        ReadingGroup(group.kind, tuple(views[id_] for id_ in group.paper_ids))
        for group in order.groups
    )
    return ReadingRoute(
        topic.id, topic.topic_hash, groups, tuple(views[id_] for id_ in order.ungrouped), len(views)
    )
