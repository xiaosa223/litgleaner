"""把一份推送渲染成飞书卡片（JSON 2.0）。私有渠道每篇带「加入 Zotero / 不感兴趣」按钮。

论文标题、摘要和模型写的内容都是不可信文本，一律放进 plain_text，不交给 markdown 解析，
这样里面的链接、@ 人、格式符号都不会生效。唯一的链接是按 DOI 拼出来的 doi.org 地址。
卡片 JSON 上限约 30 KB，每段文字都截断。
"""

import json
from typing import Any
from urllib.parse import quote

from litgleaner.domain.digest import DigestView, PaperCard, PublicCard
from litgleaner.domain.errors import PermanentError
from litgleaner.domain.library import Action, CardAction, encode_action
from litgleaner.ports.publisher import Rendered

TEXT_LIMIT = 300
MAX_CARD_BYTES = 28_000
SAVE_LABEL = "加入 Zotero"
_STATE_TEXT = {
    "saved": "✅ 已加入 Zotero",
    "queued": "⏳ 已记下，等待加入 Zotero（Zotero 没开时，打开后下次运行自动加入）",
    "dismissed": "已标记为不感兴趣",
}


def _clip(text: str | None, limit: int = TEXT_LIMIT) -> str | None:
    if not text:
        return None
    cleaned = " ".join(text.split())
    return cleaned if len(cleaned) <= limit else cleaned[:limit] + "…"


def _text(content: str, size: str = "normal", color: str | None = None) -> dict[str, Any]:
    text: dict[str, Any] = {"tag": "plain_text", "content": content, "text_size": size}
    if color:
        text["text_color"] = color
    return {"tag": "div", "text": text}


def _button(label: str, kind: str, behavior: dict[str, Any]) -> dict[str, Any]:
    return {
        "tag": "button",
        "text": {"tag": "plain_text", "content": label},
        "type": kind,
        "size": "small",
        "behaviors": [behavior],
    }


def _lines(card: PaperCard | PublicCard) -> list[dict[str, Any]]:
    label = card.tier_label or card.tier
    blocks = [_text(f"[{card.score}分·{label}] {_clip(card.title, 200)}", "heading")]
    authors = "、".join(card.authors[:3]) if card.authors else None
    meta = " · ".join(part for part in (authors, card.venue, card.published_at) if part)
    if meta:
        blocks.append(_text(_clip(meta, 200) or "", "notation", "grey"))
    for name, value in (
        ("总结", card.summary),
        ("创新", card.innovation),
        ("结论", card.conclusion),
    ):
        clipped = _clip(value)
        if clipped:
            blocks.append(_text(f"{name}：{clipped}"))
    if card.limitations:
        blocks.append(_text("不足：" + "；".join(filter(None, map(_clip, card.limitations[:2])))))
    if isinstance(card, PaperCard) and card.upgraded:
        blocks.append(_text("读完全文后升级", "notation", "green"))
    if isinstance(card, PaperCard) and not card.fulltext_used:
        blocks.append(_text("注意：只读了摘要，分数是临时的", "notation", "orange"))
    if isinstance(card, PaperCard) and card.undetermined:
        note = "注意：无法判断：" + "、".join(card.undetermined)
        blocks.append(_text(note, "notation", "orange"))
    return blocks


def _actions(card: PaperCard, view: DigestView) -> list[dict[str, Any]]:
    state = view.states.get(card.paper_id)
    if state:
        return [_text(_STATE_TEXT[state], "notation", "green" if state == "saved" else "grey")]
    buttons = []
    choices: tuple[tuple[Action, str, str], ...] = (
        ("save_to_library", SAVE_LABEL, "primary_filled"),
        ("dismiss", "不感兴趣", "default"),
    )
    for action, label, kind in choices:
        value = encode_action(CardAction(action, card.paper_id, view.digest_id))
        buttons.append(_button(label, kind, {"type": "callback", "value": value}))
    if card.doi:
        url = f"https://doi.org/{quote(card.doi, safe='/')}"
        buttons.append(_button("打开原文", "default", {"type": "open_url", "default_url": url}))
    return [{"tag": "column_set", "columns": [_column(button) for button in buttons]}]


def _column(element: dict[str, Any]) -> dict[str, Any]:
    return {"tag": "column", "width": "auto", "elements": [element]}


def build_card(view: DigestView) -> dict[str, Any]:
    elements: list[dict[str, Any]] = []
    for index, card in enumerate(view.cards):
        if index:
            elements.append({"tag": "hr"})
        elements.extend(_lines(card))
        # 公开渠道没有按钮：反馈和“加入文献库”只属于本人。
        if isinstance(card, PaperCard) and view.visibility == "private":
            elements.extend(_actions(card, view))
    if not view.cards:
        elements.append(_text("这一期没有新论文"))
    return {
        "schema": "2.0",
        # 卡片要在按钮点过之后由程序更新，必须是共享卡片。
        "config": {"update_multi": True},
        "header": {
            "title": {"tag": "plain_text", "content": f"{view.topic_name} · {view.period}"},
            "template": "blue",
        },
        "body": {"elements": elements},
    }


class FeishuCardRenderer:
    def render(self, view: DigestView) -> Rendered:
        body = json.dumps(build_card(view), ensure_ascii=False, separators=(",", ":"))
        if len(body.encode("utf-8")) > MAX_CARD_BYTES:
            raise PermanentError(
                f"飞书卡片超过 {MAX_CARD_BYTES} 字节（{len(body.encode())}），请减小路由的 limit"
            )
        return Rendered(title=f"{view.topic_name} · {view.period}", body=body)
