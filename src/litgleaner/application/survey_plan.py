"""调研配置指纹、课题校验与论文身份解析。"""

from litgleaner.application.context import AppContext
from litgleaner.domain.errors import InvalidRequest, NotFound
from litgleaner.domain.hashing import stable_hash
from litgleaner.domain.identity import (
    ExistingPaper,
    Resolution,
    decide_resolution,
    record_aliases,
    title_key,
)
from litgleaner.domain.paper import SourceRecord
from litgleaner.domain.survey import SurveyPreview, SurveySettings
from litgleaner.domain.topic import Topic


def require_topic(ctx: AppContext, topic_id: str, *, active: bool = False) -> Topic:
    topic = ctx.topics.get(topic_id)
    if topic is None:
        raise NotFound(f"课题不存在：{topic_id}")
    state = ctx.store.judgments.get_topic_state(topic_id)
    if active and (state is None or state.status != "active"):
        raise InvalidRequest("请先启用这个课题，再确认执行调研")
    return topic


def fingerprint(ctx: AppContext, topic: Topic, settings: SurveySettings) -> str:
    rubric = ctx.rubrics[topic.rubric]
    models = {
        task: None if route is None else (route.model, route.prompt.version, route.provider_version)
        for task in ("triage", "extract", "assess")
        for route in (ctx.llm.routes.get(task),)
    }
    return stable_hash(
        {
            "topic": topic.topic_hash,
            "seeds": topic.seeds.model_dump(mode="json"),
            "survey": settings.model_dump(mode="json"),
            "models": models,
            "semantic": rubric.semantic_hash,
            "policy": rubric.policy_hash,
            "language": ctx.settings.reading_language,
            "chunker": ctx.settings.chunker.version,
            "token_budget": ctx.settings.assess_token_budget,
            "parser": ctx.parser.parser_id,
            "sources": {
                name: sorted(t.__name__ for t in source.capabilities)
                for name, source in ctx.sources.items()
            },
            "sources_version": ctx.settings.survey_sources_version,
            "fulltext": [provider.name for provider in ctx.fulltext],
        }
    )


def resolution(ctx: AppContext, record: SourceRecord) -> Resolution:
    papers = ctx.store.papers
    aliases = papers.find_ids_by_aliases(record_aliases(record))
    matches = (
        ()
        if aliases
        else tuple(
            ExistingPaper(paper.id, paper.authors)
            for paper in papers.find_by_title_key(title_key(record.title))
        )
    )
    return decide_resolution(record, aliases, matches)


def assert_current(ctx: AppContext, plan: SurveyPreview) -> Topic:
    topic = require_topic(ctx, plan.topic_id, active=True)
    if (
        plan.fingerprint != fingerprint(ctx, topic, plan.settings)
        or plan.settings != ctx.settings.survey
    ):
        raise InvalidRequest("课题、来源、模型或调研配置已变化，请重新预览后确认")
    return topic
