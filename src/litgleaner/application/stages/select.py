"""选文阶段：生成不可变的 Digest。内容一旦生成，后续改分数也不会变。"""

from collections.abc import Iterable, Mapping
from typing import Any

from ulid import ULID

from litgleaner.application.cards import build_card
from litgleaner.application.context import AppContext
from litgleaner.application.stages.base import Stage
from litgleaner.application.topics import active_topics
from litgleaner.domain.hashing import stable_hash
from litgleaner.domain.routing import Route, expand_topics, schedule_slot
from litgleaner.domain.topic import Topic
from litgleaner.ports.store import DigestRecord, JobSpec, RankQuery, ScoredAssessment


def _candidates(ctx: AppContext, route: Route, topic: Topic) -> tuple[ScoredAssessment, ...]:
    rubric = ctx.rubrics[topic.rubric]
    return ctx.store.judgments.rank_candidates(
        RankQuery(
            topic_id=topic.id,
            topic_hash=topic.topic_hash,
            semantic_hash=rubric.semantic_hash,
            policy_hash=rubric.policy_hash,
            tiers=route.tiers,
            channel=route.channel,
            limit=route.limit,
        )
    )


def _route_topics(ctx: AppContext, route: Route) -> tuple[Topic, ...]:
    channel = ctx.channels.get(route.channel)
    if channel is None:
        return ()
    topics = expand_topics(route.topic, active_topics(ctx))
    if channel.visibility == "public":
        return tuple(topic for topic in topics if topic.public)
    return topics


class SelectStage(Stage):
    name = "select"

    def plan(self, ctx: AppContext) -> Iterable[JobSpec]:
        for route in ctx.routes:
            slot = schedule_slot(route.schedule, ctx.clock(), ctx.settings.timezone)
            if slot is None:
                continue
            for topic in _route_topics(ctx, route):
                if ctx.store.outputs.digest_exists(route.id, topic.id, slot):
                    continue
                found = _candidates(ctx, route, topic)
                if found:
                    ids = stable_hash([item.assessment.id for item in found])
                    yield JobSpec(
                        key=f"select:{route.id}:{topic.id}:{slot}:{ids}",
                        stage=self.name,
                        payload={"route_id": route.id, "topic_id": topic.id, "period": slot},
                    )

    def process(self, ctx: AppContext, payload: Mapping[str, Any]) -> None:
        route = next(item for item in ctx.routes if item.id == payload["route_id"])
        topic = ctx.topics[payload["topic_id"]]
        period = payload["period"]
        if ctx.store.outputs.digest_exists(route.id, topic.id, period):
            return
        rubric = ctx.rubrics[topic.rubric]
        items = [
            (scored.assessment.paper_id, build_card(ctx, scored, rubric))
            for scored in _candidates(ctx, route, topic)
        ]
        if not items:  # 候选已被同渠道的其他路由取走
            return
        record = DigestRecord(str(ULID()), route.id, topic.id, route.channel, period)
        ctx.store.outputs.create_digest(record, items, ctx.now_iso())
