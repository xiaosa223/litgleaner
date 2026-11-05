"""算分阶段：纯本地计算，权重变化后重新运行即可，不产生 LLM 费用。"""

from collections.abc import Iterable, Mapping
from typing import Any

from litfactory.application.context import AppContext
from litfactory.application.stages.base import Stage
from litfactory.application.topics import active_topics
from litfactory.domain.rubric import compute_score
from litfactory.ports.store import JobSpec


class ScoreStage(Stage):
    name = "score"

    def plan(self, ctx: AppContext) -> Iterable[JobSpec]:
        for topic in active_topics(ctx).values():
            rubric = ctx.rubrics[topic.rubric]
            pending = ctx.store.judgments.unscored_assessments(
                topic.id, rubric.semantic_hash, rubric.policy_hash
            )
            for assessment_id in pending:
                yield JobSpec(
                    key=f"score:{assessment_id}:{rubric.policy_hash}",
                    stage=self.name,
                    payload={"assessment_id": assessment_id, "topic_id": topic.id},
                )

    def process(self, ctx: AppContext, payload: Mapping[str, Any]) -> None:
        assessment = ctx.store.judgments.get_assessment(payload["assessment_id"])
        if assessment is None:
            raise ValueError(f"评估不存在：{payload['assessment_id']}")
        rubric = ctx.rubrics[ctx.topics[payload["topic_id"]].rubric]
        result = compute_score(rubric, assessment.levels)
        ctx.store.judgments.save_score(assessment.id, rubric.policy_hash, result, ctx.now_iso())
