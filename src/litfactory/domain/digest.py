"""推送内容。公开卡片是字段白名单：新字段默认不会公开。"""

import re
from dataclasses import dataclass

_URL = re.compile(r"(?:https?://|www\.)\S+", re.IGNORECASE)
_URL_REMOVED = "[链接已移除]"


def strip_urls(text: str | None) -> str | None:
    """模型输出来自不可信的论文内容，里面的链接不能发给任何人。不含链接前缀的裸域名不处理。"""
    return _URL.sub(_URL_REMOVED, text) if text else text


@dataclass(frozen=True)
class PaperCard:
    paper_id: str
    title: str
    authors: tuple[str, ...]
    venue: str | None
    published_at: str | None
    doi: str | None
    score: int
    tier: str
    provisional: bool
    summary: str | None
    conclusion: str | None
    limitations: tuple[str, ...]
    innovation: str | None
    abstract: str | None
    quotes: tuple[str, ...]
    fulltext_used: bool
    parse_quality: str | None
    tier_label: str = ""


@dataclass(frozen=True)
class PublicCard:
    title: str
    authors: tuple[str, ...]
    venue: str | None
    published_at: str | None
    doi: str | None
    score: int
    tier: str
    summary: str | None
    conclusion: str | None
    limitations: tuple[str, ...]
    innovation: str | None
    tier_label: str = ""


def to_public_card(card: PaperCard) -> PublicCard:
    return PublicCard(
        title=card.title,
        authors=card.authors,
        venue=card.venue,
        published_at=card.published_at,
        doi=card.doi,
        score=card.score,
        tier=card.tier,
        summary=card.summary,
        conclusion=card.conclusion,
        limitations=card.limitations,
        innovation=card.innovation,
        tier_label=card.tier_label,
    )


@dataclass(frozen=True)
class DigestView:
    digest_id: str
    topic_name: str
    period: str
    visibility: str
    cards: tuple[PaperCard | PublicCard, ...]
