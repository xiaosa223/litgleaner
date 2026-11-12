"""推送内容。公开卡片是字段白名单：新字段默认不会公开。"""

import re
from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Literal

_URL = re.compile(r"(?:https?://|www\.)\S+", re.IGNORECASE)
_URL_REMOVED = "[链接已移除]"


def strip_urls(text: str | None) -> str | None:
    """模型输出来自不可信的论文内容，里面的链接不能发给任何人。不含链接前缀的裸域名不处理。"""
    return _URL.sub(_URL_REMOVED, text) if text else text


@dataclass(frozen=True)
class MatchedTopic:
    topic_id: str
    name: str


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
    undetermined: tuple[str, ...] = ()  # 无法判断的维度名称；不在公开白名单里
    upgraded: bool = False  # 之前以临时分推过，读完全文后升级再推一次
    matched_topics: tuple[MatchedTopic, ...] = ()  # 随推送冻结，仅用于私有渠道


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


# 用户对每篇论文做过什么：已加入文献库、已选择但还在写入、不感兴趣。没操作过的不出现。
PaperState = Literal["saved", "queued", "dismissed"]


@dataclass(frozen=True)
class DigestView:
    digest_id: str
    topic_name: str
    period: str
    visibility: str
    cards: tuple[PaperCard | PublicCard, ...]
    states: Mapping[str, PaperState] = field(default_factory=dict)  # 键是 paper_id
