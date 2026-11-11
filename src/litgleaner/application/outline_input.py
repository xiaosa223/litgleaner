"""综述只读已读来源；冻结正文输入与代码生成的表格、引用附件。"""

import json
from collections.abc import Mapping
from dataclasses import asdict, dataclass
from typing import Any

from litgleaner.application.context import AppContext
from litgleaner.application.qa_context import qa_token_count
from litgleaner.application.topic_export import comparison_row, snapshot
from litgleaner.domain.comparison_table import render_table
from litgleaner.domain.errors import InvalidRequest
from litgleaner.domain.evidence import wrap_untrusted
from litgleaner.domain.export_references import render_references
from litgleaner.domain.hashing import stable_hash
from litgleaner.domain.language import language_name
from litgleaner.domain.outline import OutlinePaper
from litgleaner.domain.paper import Paper
from litgleaner.domain.topic import Topic
from litgleaner.ports.export import TopicExportSnapshot


@dataclass(frozen=True)
class OutlineInput:
    text: str
    system: str
    tokens: int
    input_hash: str
    considered_ids: tuple[str, ...]
    paper_ids: tuple[str, ...]
    omitted_ids: tuple[str, ...]
    source_hashes: dict[str, str]
    papers: tuple[OutlinePaper, ...]
    eligible_gap_ids: tuple[str, ...]
    warnings: tuple[str, ...]


def producer_version(ctx: AppContext) -> str | None:
    route = ctx.llm.routes.get("outline")
    return (
        None
        if route is None
        else stable_hash(
            {
                "contract": "outline-v1",
                "model": route.model,
                "prompt": route.prompt.version,
                "provider": route.provider_version,
            }
        )
    )


def source_paper(
    ctx: AppContext, topic: Topic, paper: Paper, saved: TopicExportSnapshot
) -> tuple[OutlinePaper, str]:
    row = comparison_row(ctx, topic, paper, saved)
    refs = render_references(((paper, saved.aliases.get(paper.id, ())),))
    attachment = OutlinePaper(
        paper_id=paper.id,
        title=paper.title,
        table_markdown=render_table((row,), "md"),
        bibtex=refs.bibtex,
        csl_json=refs.csl_json,
        needs_verification=paper.id in refs.unverified_ids,
        reference_warnings=refs.warnings,
    )
    extraction = saved.extractions.get(paper.id)
    value = stable_hash(
        {
            "attachment": attachment.model_dump(),
            "extraction": None
            if extraction is None
            else {
                "input": extraction.input_hash,
                "producer": extraction.extractor_version,
                "data": dict(extraction.data),
            },
            "fulltext": None if (state := saved.fulltexts.get(paper.id)) is None else asdict(state),
        }
    )
    return attachment, value


def read_sources(ctx: AppContext, topic: Topic) -> tuple[TopicExportSnapshot, tuple[Paper, ...]]:
    saved = snapshot(ctx, topic)
    papers = tuple(
        paper
        for paper in saved.papers
        if ((state := saved.reading.get(paper.id)) is not None and state.status == "read")
    )
    return saved, papers


def _claim(value: Any) -> dict[str, Any] | None:
    if not isinstance(value, Mapping):
        return None
    return {
        "text": str(value.get("text", ""))[:500],
        "quote": str(value.get("quote", ""))[:500],
        "page": value.get("page"),
        "verified": value.get("verified") is True,
    }


def _verified(value: Any) -> bool:
    return (
        isinstance(value, Mapping)
        and value.get("verified") is True
        and isinstance(value.get("text"), str)
        and bool(value["text"].strip())
        and isinstance(value.get("quote"), str)
        and bool(value["quote"].strip())
        and type(value.get("page")) is int
        and value["page"] >= 0
    )


def _complete(value: Any) -> bool:
    return _verified(value) and len(value["text"]) <= 500 and len(value["quote"]) <= 500


def paper_record(
    ctx: AppContext, topic: Topic, paper: Paper, saved: TopicExportSnapshot
) -> dict[str, Any]:
    row = comparison_row(ctx, topic, paper, saved)
    data = row.extraction or {}
    datasets = data.get("datasets", [])
    datasets = datasets if isinstance(datasets, list | tuple) else []
    eligible = (
        not row.extraction_stale
        and _complete(data.get("method"))
        and 0 < len(datasets) <= 8
        and all(_complete(item) for item in datasets)
    )
    return {
        "paper_id": paper.id,
        "title": paper.title[:1000],
        "extraction_stale": row.extraction_stale,
        "method": _claim(data.get("method")),
        "datasets": [_claim(item) for item in datasets[:8]],
        "goal_ids": [link.goal_id for link in row.goal_links],
        "eligible_gap_evidence": eligible,
    }


def input_hash(text: str, system: str, budget: int, sources: dict[str, str], language: str) -> str:
    return stable_hash(
        {"text": text, "system": system, "budget": budget, "sources": sources, "language": language}
    )


def _json(profile: str, records: list[dict[str, Any]]) -> str:
    return json.dumps({"topic_profile": profile, "papers": records}, ensure_ascii=False)


def _select(
    ctx: AppContext,
    topic: Topic,
    saved: TopicExportSnapshot,
    papers: tuple[Paper, ...],
    system: str,
) -> OutlineInput:
    profile, budget = topic.profile_text(), ctx.settings.outline.token_budget
    records: list[dict[str, Any]] = []
    hashes: dict[str, str] = {}
    attachments: list[OutlinePaper] = []
    omitted: list[str] = []
    eligible: list[str] = []
    for paper in papers:
        record = paper_record(ctx, topic, paper, saved)
        text = _json(profile, [*records, record])
        if qa_token_count(system + wrap_untrusted(text)) > budget:
            omitted.append(paper.id)
            continue
        attachment, value = source_paper(ctx, topic, paper, saved)
        records.append(record)
        hashes[paper.id] = value
        attachments.append(attachment)
        if record["eligible_gap_evidence"]:
            eligible.append(paper.id)
    warnings = [
        "分组和覆盖缺口候选为模型判断，仅限当前实际发送的已读集合",
        "标题、方法与数据集按限长发送；截断、遗漏、未核验或过期证据不支持覆盖缺口候选",
    ]
    if omitted:
        warnings.append("达到综述输入预算，遗漏论文不参与分组或覆盖缺口判断")
    text = _json(profile, records)
    return OutlineInput(
        text,
        system,
        qa_token_count(system + wrap_untrusted(text)),
        input_hash(text, system, budget, hashes, ctx.settings.reading_language),
        tuple(paper.id for paper in papers),
        tuple(hashes),
        tuple(omitted),
        hashes,
        tuple(attachments),
        tuple(eligible),
        tuple(warnings),
    )


def prepare_input(ctx: AppContext, topic: Topic) -> OutlineInput:
    route = ctx.llm.routes.get("outline")
    system = (
        ""
        if route is None
        else route.prompt.render(
            {
                "output_language": language_name(ctx.settings.reading_language),
            }
        )
    )
    budget = ctx.settings.outline.token_budget
    if qa_token_count(system + wrap_untrusted(_json(topic.profile_text(), []))) > budget:
        raise InvalidRequest("研究画像超过综述输入预算，请缩短画像或提高 outline.token_budget")
    saved, papers = read_sources(ctx, topic)
    return _select(ctx, topic, saved, papers, system)
