"""纯文本渲染。公开卡片没有的字段不会出现。"""

from litfactory.domain.digest import DigestView, PaperCard, PublicCard
from litfactory.ports.publisher import Rendered


def _card_lines(card: PaperCard | PublicCard) -> list[str]:
    lines = [f"[{card.score}分·{card.tier_label or card.tier}] {card.title}"]
    if card.authors:
        lines.append("作者：" + "、".join(card.authors))
    if card.venue or card.published_at:
        lines.append("来源：" + " ".join(part for part in (card.venue, card.published_at) if part))
    if card.doi:
        lines.append(f"https://doi.org/{card.doi}")
    if card.innovation:
        lines.append(f"创新：{card.innovation}")
    if card.conclusion:
        lines.append(f"结论：{card.conclusion}")
    lines.extend(f"不足：{item}" for item in card.limitations)
    return lines


def _notes(card: PaperCard | PublicCard) -> list[str]:
    if not isinstance(card, PaperCard):
        return []
    notes = []
    if not card.fulltext_used:
        notes.append("注意：只读了摘要，分数是临时的")
    if card.parse_quality == "degraded":
        notes.append("注意：解析质量较低")
    return notes


class PlainTextRenderer:
    def render(self, view: DigestView) -> Rendered:
        title = f"{view.topic_name} · {view.period}"
        blocks = ["\n".join([*_card_lines(card), *_notes(card)]) for card in view.cards]
        return Rendered(title=title, body="\n\n".join([title, *blocks]))
