"""显式调研的来源采集：只归档原始响应，不写论文、任务或来源游标。"""

import hashlib
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from datetime import date
from typing import Any

from litgleaner.application.codec import compress
from litgleaner.application.context import AppContext
from litgleaner.application.source_fetch import fetch, scopes
from litgleaner.application.survey_plan import fingerprint
from litgleaner.domain.errors import InvalidRequest, SchemaChanged
from litgleaner.domain.hashing import stable_hash
from litgleaner.domain.identity import Alias, normalize_doi, record_aliases, title_key
from litgleaner.domain.paper import SourceRecord
from litgleaner.domain.query import HistoricalCitations, HistoricalSearch, Query, ReferenceList
from litgleaner.domain.reading_route import recent_since
from litgleaner.domain.survey import (
    SurveyCandidate,
    SurveyEdge,
    SurveyMode,
    SurveySettings,
    SurveySource,
)
from litgleaner.domain.survey_candidates import CandidatePool
from litgleaner.domain.topic import Topic
from litgleaner.ports.source import FetchResult, Source
from litgleaner.ports.source_capture import CaptureScope


@dataclass(frozen=True)
class SurveyCollected:
    candidates: tuple[SurveyCandidate, ...]
    edges: tuple[SurveyEdge, ...]
    sources: tuple[SurveySource, ...]
    uncovered: tuple[str, ...]


def _history(
    topic: Topic, settings: SurveySettings, today: date
) -> Iterable[tuple[SurveyMode, Query]]:
    for category in ("reviews", "highly_cited", "recent"):
        yield (
            category,
            HistoricalSearch(
                terms=topic.query.terms,
                exclude=topic.query.exclude,
                since=max(settings.since, recent_since(today))
                if category == "recent"
                else settings.since,
                until=today,
                category=category,
                limit=settings.category_limit,
            ),
        )


def _archive(ctx: AppContext, source: str, query: Query, raw: bytes | None) -> str | None:
    if raw is None:
        return None
    digest = hashlib.sha256(raw).hexdigest()
    key = f"survey_raw/{stable_hash(source)[:16]}/{query.hash}/{digest}.zst"
    ctx.blobs.put(key, compress(raw))
    return key


def _reference_data(
    name: str, query: ReferenceList, result: FetchResult, limit: int, pool: CandidatePool
) -> tuple[list[tuple[SourceRecord, tuple[str, ...]]], tuple[SurveyEdge, ...]]:
    chosen = result.references[:limit]
    if any(
        normalize_doi(item.citing_doi) not in query.dois or not item.raw_text.strip()
        for item in chosen
    ):
        raise SchemaChanged("参考文献缺少原始条目或主论文不属于本次查询")
    edges = tuple(
        SurveyEdge(
            source=name,
            citing_aliases=(Alias("doi", normalize_doi(item.citing_doi) or ""),),
            cited_aliases=item.cited_aliases,
            source_position=item.position,
            raw_text=item.raw_text,
            contexts=item.contexts,
            intents=item.intents,
            influential=item.influential,
        )
        for item in chosen
    )
    records = []
    for record in result.records:
        aliases = pool.aliases(record)
        seeds = tuple(
            sorted(
                {
                    normalize_doi(item.citing_doi) or ""
                    for item in chosen
                    if aliases.intersection(item.cited_aliases)
                    or (
                        not item.cited_aliases
                        and item.cited_title
                        and title_key(item.cited_title) == title_key(record.title)
                    )
                }
            )
        )
        if seeds:
            records.append((record, seeds))
        if len(records) >= limit:
            break
    return records, edges


def _citation_data(
    name: str, query: HistoricalCitations, records: tuple[SourceRecord, ...]
) -> tuple[SurveyEdge, ...]:
    return tuple(
        SurveyEdge(
            source=name,
            citing_aliases=record_aliases(record),
            cited_aliases=(Alias("doi", query.dois[0]),),
            source_position=index,
            raw_text=record.model_dump_json(),
        )
        for index, record in enumerate(records)
        if record_aliases(record)
    )


def _capture(
    ctx: AppContext,
    name: str,
    source: Source,
    task: tuple[SurveyMode, Query],
    settings: SurveySettings,
    *,
    pool: CandidatePool,
    scope: CaptureScope | None,
) -> tuple[SurveySource, tuple[SurveyEdge, ...]]:
    mode, query = task
    try:
        result = fetch(ctx, source, query, None, scope)
        report = _report(ctx, name, task, settings, result)
    except Exception as error:
        return SurveySource(
            source=name, mode=mode, query_hash=query.hash, fetched=0, error=type(error).__name__
        ), ()
    if report.error:
        return report, ()
    limit = (
        settings.category_limit if isinstance(query, HistoricalSearch) else settings.expansion_limit
    )
    try:
        if isinstance(query, ReferenceList):
            records, edges = _reference_data(name, query, result, limit, pool)
        else:
            records = [(record, ()) for record in result.records[:limit]]
            edges = (
                _citation_data(name, query, result.records[:limit])
                if isinstance(query, HistoricalCitations)
                else ()
            )
    except (ValueError, TypeError, AttributeError, SchemaChanged) as error:
        return report.model_copy(update={"error": type(error).__name__}), ()
    for record, seeds in records:
        pool.add(record, name, mode, seeds)
    return report, edges


def _report(
    ctx: AppContext,
    name: str,
    task: tuple[SurveyMode, Query],
    settings: SurveySettings,
    result: FetchResult,
) -> SurveySource:
    mode, query = task
    limit = (
        settings.category_limit if isinstance(query, HistoricalSearch) else settings.expansion_limit
    )
    fetched = len(result.references) if isinstance(query, ReferenceList) else len(result.records)
    warnings = result.warnings
    if fetched > limit:
        boundary = "category_limit" if isinstance(query, HistoricalSearch) else "expansion_limit"
        warnings += (f"达到调研 {boundary}={limit} 原始条目上限，已截断",)
    error, raw_key = None, None
    try:
        raw_key = _archive(ctx, name, query, result.raw)
    except Exception as failure:
        error = type(failure).__name__
    return SurveySource(
        source=name,
        mode=mode,
        query_hash=query.hash,
        fetched=fetched,
        truncated=result.truncated or fetched > limit,
        warnings=warnings,
        raw_key=raw_key,
        error=error,
    )


def _seeds(
    dois: tuple[str, ...], settings: SurveySettings, uncovered: list[str]
) -> tuple[str, ...]:
    normalized = {value for raw in dois if (value := normalize_doi(raw))}
    if any(normalize_doi(raw) is None for raw in dois):
        uncovered.append("部分种子 DOI 无效，未发起对应查询")
    if len(normalized) > settings.seed_limit:
        uncovered.append(f"种子 DOI 达到 seed_limit={settings.seed_limit}，其余未覆盖")
    return tuple(sorted(normalized))[: settings.seed_limit]


def _tasks(
    topic: Topic, settings: SurveySettings, today: date, seeds: tuple[str, ...], historical: bool
) -> Iterable[tuple[SurveyMode, Query]]:
    if historical and any(term.strip() for term in topic.query.terms):
        yield from _history(topic, settings, today)
    for doi in seeds:
        yield "references", ReferenceList(dois=(doi,))
        yield (
            "citations",
            HistoricalCitations(
                dois=(doi,), since=settings.since, until=today, limit=settings.expansion_limit
            ),
        )


def _available(ctx: AppContext, query: Query) -> list[tuple[str, Source]]:
    return [(n, s) for n, s in sorted(ctx.sources.items()) if type(query) in s.capabilities]


def _capture_scopes(
    ctx: AppContext,
    topic: Topic,
    settings: SurveySettings,
    tasks: tuple[tuple[SurveyMode, Query], ...],
    owner: Mapping[str, Any],
) -> dict[tuple[SurveyMode, str, str], CaptureScope]:
    if ctx.source_capture is None:
        return {}
    calls = [(mode, name, query) for mode, query in tasks for name, _ in _available(ctx, query)]
    planned = scopes(
        "survey",
        {
            **owner,
            "topic": topic.model_dump(mode="json"),
            "topic_id": topic.id,
            "topic_hash": topic.topic_hash,
            "fingerprint": fingerprint(ctx, topic, settings),
            "settings": settings.model_dump(mode="json"),
            "timezone": str(ctx.settings.timezone),
            "tasks": [{"mode": mode, "query": q.model_dump(mode="json")} for mode, q in tasks],
        },
        [(name, query, None) for _, name, query in calls],
    )
    return {
        (mode, query.hash, name): scope
        for (mode, name, query), scope in zip(calls, planned, strict=True)
    }


def collect(
    ctx: AppContext,
    topic: Topic,
    settings: SurveySettings,
    *,
    dois: tuple[str, ...],
    historical: bool = True,
    capture_owner: Mapping[str, Any] | None = None,
) -> SurveyCollected:
    """第一轮含三类历史查询，后续轮只扩展调用者明确传入的高分 DOI。"""
    today = ctx.clock().astimezone(ctx.settings.timezone).date()
    if settings.since > today:
        raise InvalidRequest("调研开始日期不能晚于今天")
    uncovered: list[str] = []
    seeds = _seeds(dois, settings, uncovered)
    tasks = tuple(_tasks(topic, settings, today, seeds, historical))
    group = _capture_scopes(
        ctx,
        topic,
        settings,
        tasks,
        {
            **(capture_owner or {}),
            "seeds": list(dois),
            "historical": historical,
            "today": today.isoformat(),
        },
    )
    if historical and not any(term.strip() for term in topic.query.terms):
        uncovered.append("未配置非空关键词，未进行历史检索")
    return _collect_tasks(ctx, settings, tasks, group, uncovered)


def _collect_tasks(
    ctx: AppContext,
    settings: SurveySettings,
    tasks: tuple[tuple[SurveyMode, Query], ...],
    group: Mapping[tuple[SurveyMode, str, str], CaptureScope],
    uncovered: list[str],
) -> SurveyCollected:
    pool = CandidatePool()
    reports: list[SurveySource] = []
    edges: list[SurveyEdge] = []
    for task in tasks:
        mode, query = task
        available = _available(ctx, query)
        if not available:
            uncovered.append(f"{mode} 没有启用支持该能力的来源")
        for name, source in available:
            report, found_edges = _capture(
                ctx,
                name,
                source,
                task,
                settings,
                pool=pool,
                scope=group.get((mode, query.hash, name)),
            )
            reports.append(report)
            edges.extend(found_edges)
            if report.error:
                uncovered.append(f"{name}/{mode} 查询失败：{report.error}")
            if report.truncated:
                uncovered.append(f"{name}/{mode} 达到来源或调研上限，未覆盖剩余结果")
    candidates = pool.candidates()
    if len(candidates) > settings.candidate_limit:
        uncovered.append(f"最终候选达到 candidate_limit={settings.candidate_limit}，其余未纳入本轮")
    return SurveyCollected(
        candidates[: settings.candidate_limit],
        tuple(edges),
        tuple(reports),
        tuple(dict.fromkeys(uncovered)),
    )
