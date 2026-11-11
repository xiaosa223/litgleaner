"""固定地图输入及预算；未发送的论文单独列出，不允许模型为其归组。"""

import json
from dataclasses import dataclass
from typing import Any

from litgleaner.application.context import AppContext
from litgleaner.application.qa_context import qa_token_count
from litgleaner.domain.errors import InvalidRequest
from litgleaner.domain.evidence import wrap_untrusted
from litgleaner.domain.hashing import stable_hash
from litgleaner.domain.language import language_name
from litgleaner.domain.paper import Paper
from litgleaner.domain.topic import Topic
from litgleaner.ports.store import Extraction
from litgleaner.ports.topic_map import MapSnapshot


@dataclass(frozen=True)
class MapInput:
    text: str
    system: str
    tokens: int
    input_hash: str
    paper_ids: tuple[str, ...]
    omitted_ids: tuple[str, ...]
    source_hashes: dict[str, str]
    warnings: tuple[str, ...]


def producer_version(ctx: AppContext) -> str | None:
    route = ctx.llm.routes.get("map")
    return (
        None
        if route is None
        else stable_hash(
            {
                "version": "topic-map-v1",
                "model": route.model,
                "prompt": route.prompt.version,
                "provider": route.provider_version,
            }
        )
    )


def source_hash(paper: Paper, extraction: Extraction | None) -> str:
    return stable_hash(
        {
            "title": paper.title,
            "abstract": paper.abstract,
            "extraction": None
            if extraction is None
            else {
                "input": extraction.input_hash,
                "producer": extraction.extractor_version,
                "data": dict(extraction.data),
            },
        }
    )


def _record(paper: Paper, extraction: Extraction | None) -> dict[str, Any]:
    # 单篇只给摘要量级的信息，实际未发送的尾部仍能由 source_hash 检测变化。
    detail = (
        None
        if extraction is None
        else {
            key: str(extraction.data.get(key, ""))[:1000]
            for key in ("summary", "conclusion", "innovation")
        }
    )
    return {
        "paper_id": paper.id,
        "title": paper.title[:1000],
        "abstract": (paper.abstract or "")[:2000],
        "extraction": detail,
    }


def _json(profile: str, records: list[dict[str, Any]]) -> str:
    return json.dumps({"topic_profile": profile, "papers": records}, ensure_ascii=False)


def map_input_hash(
    text: str, system: str, budget: int, sources: dict[str, str], language: str
) -> str:
    return stable_hash(
        {"text": text, "system": system, "budget": budget, "sources": sources, "language": language}
    )


def prepare_input(ctx: AppContext, topic: Topic, snapshot: MapSnapshot) -> MapInput:
    route = ctx.llm.routes.get("map")
    system = (
        ""
        if route is None
        else route.prompt.render(
            {
                "output_language": language_name(ctx.settings.reading_language),
            }
        )
    )
    budget = ctx.settings.map.token_budget
    records: list[dict[str, Any]] = []
    hashes: dict[str, str] = {}
    omitted: list[str] = []
    warnings: list[str] = []
    profile = topic.profile_text()
    if qa_token_count(system + wrap_untrusted(_json(profile, []))) > budget:
        raise InvalidRequest("研究画像超过地图输入预算，请缩短画像或提高 map.token_budget")
    for paper in snapshot.papers:
        extraction = snapshot.extractions.get(paper.id)
        record = _record(paper, extraction)
        text = _json(profile, [*records, record])
        if qa_token_count(system + wrap_untrusted(text)) > budget:
            omitted.append(paper.id)
            continue
        records.append(record)
        hashes[paper.id] = source_hash(paper, extraction)
        if len(paper.title) > 1000 or len(paper.abstract or "") > 2000 or extraction is not None:
            warnings.append("单篇输入按标题、摘要和提取摘要限长，未发送完整正文或完整提取")
    text = _json(profile, records)
    if omitted:
        warnings.append("达到地图输入预算，未发送论文保持未分组，可提高预算后重新预览")
    return MapInput(
        text,
        system,
        qa_token_count(system + wrap_untrusted(text)),
        map_input_hash(text, system, budget, hashes, ctx.settings.reading_language),
        tuple(hashes),
        tuple(omitted),
        hashes,
        tuple(dict.fromkeys(warnings)),
    )
