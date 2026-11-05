"""把判断结果组装成推送卡片和归档条目。"""

from collections.abc import Mapping
from dataclasses import asdict
from typing import Any

from litfactory.application.context import AppContext
from litfactory.domain.digest import PaperCard, strip_urls
from litfactory.domain.rubric import Rubric
from litfactory.ports.store import ScoredAssessment


def _limitation_text(item: Mapping[str, Any]) -> str:
    text = strip_urls(str(item["text"])) or ""
    if item.get("source") == "inferred":
        text = f"{text}（AI 推断）"
    elif not item.get("verified"):
        text = f"{text}（未核实）"
    return text


def _tier_label(rubric: Rubric, tier: str) -> str:
    labels = {item.key: item.label for item in rubric.tiers}
    return labels.get(tier, "需要全文" if tier == rubric.provisional.tier else tier)


def build_card(ctx: AppContext, scored: ScoredAssessment, rubric: Rubric) -> dict[str, Any]:
    paper_id = scored.assessment.paper_id
    paper = ctx.store.papers.get(paper_id)
    if paper is None:
        raise ValueError(f"论文不存在：{paper_id}")
    extraction = ctx.store.papers.get_extraction(paper_id)
    data: Mapping[str, Any] = extraction.data if extraction else {}
    fulltext = ctx.store.papers.get_fulltext(paper_id)
    aliases = {alias.scheme: alias.value for alias in ctx.store.papers.aliases_of(paper_id)}
    quotes = [
        str(item["quote"])
        for item in scored.assessment.evidence.values()
        if item.get("quote") and item.get("verified")
    ]
    card = PaperCard(
        paper_id=paper.id,
        title=paper.title,
        authors=paper.authors,
        venue=paper.venue,
        published_at=paper.published_at,
        doi=aliases.get("doi"),
        score=scored.score,
        tier=scored.tier,
        tier_label=_tier_label(rubric, scored.tier),
        provisional=scored.provisional,
        summary=strip_urls(data.get("summary")),
        conclusion=strip_urls(data.get("conclusion")),
        limitations=tuple(_limitation_text(item) for item in data.get("limitations", ())),
        innovation=strip_urls(data.get("innovation")),
        abstract=paper.abstract,
        quotes=tuple(quotes),
        fulltext_used=scored.assessment.fulltext_used,
        parse_quality=fulltext.quality if fulltext else None,
    )
    return asdict(card)


def card_from_dict(data: Mapping[str, Any]) -> PaperCard:
    return PaperCard(
        **{
            **data,
            "authors": tuple(data["authors"]),
            "limitations": tuple(data["limitations"]),
            "quotes": tuple(data["quotes"]),
        }
    )
