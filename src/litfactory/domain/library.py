"""加入文献库：要写入的条目，以及卡片按钮携带的动作。

和具体文献管理软件无关，字段映射由各适配器完成。
"""

import html
import re
from dataclasses import dataclass
from typing import Literal

from litfactory.domain.digest import PaperCard, strip_urls
from litfactory.domain.identity import Alias, normalize_arxiv_id, normalize_doi
from litfactory.domain.paper import Paper

Action = Literal["save_to_library", "dismiss"]
ACTIONS: tuple[Action, ...] = ("save_to_library", "dismiss")
# 按钮回调值要短：飞书和 Telegram 都限制回调数据长度。
_ACTION_CODES: dict[str, Action] = {"save": "save_to_library", "dismiss": "dismiss"}
_CODE_OF: dict[Action, str] = {action: code for code, action in _ACTION_CODES.items()}
_ID = re.compile(r"^[0-9A-Za-z_-]{1,64}$")
NOTE_TEXT_LIMIT = 2000


@dataclass(frozen=True)
class CardAction:
    action: Action
    paper_id: str
    digest_id: str | None = None


def encode_action(action: CardAction) -> dict[str, str]:
    value = {"a": _CODE_OF[action.action], "p": action.paper_id}
    if action.digest_id:
        value["d"] = action.digest_id
    return value


def decode_action(value: object) -> CardAction | None:
    """回调值来自外部平台，结构不对一律当作无效，不抛异常。"""
    if not isinstance(value, dict):
        return None
    action = _ACTION_CODES.get(str(value.get("a")))
    paper_id = value.get("p")
    digest_id = value.get("d")
    if action is None or not isinstance(paper_id, str) or not _ID.match(paper_id):
        return None
    if digest_id is not None and (not isinstance(digest_id, str) or not _ID.match(digest_id)):
        return None
    return CardAction(action, paper_id, digest_id)


@dataclass(frozen=True)
class LibraryEntry:
    """一篇要加入文献库的论文。key 是本系统的论文编号，用于写入去重。"""

    key: str
    title: str
    authors: tuple[str, ...]
    venue: str | None
    published_at: str | None
    doi: str | None
    arxiv_id: str | None
    abstract: str | None
    url: str | None
    tags: tuple[str, ...]
    note_html: str
    pdf: bytes | None = None

    @property
    def pdf_filename(self) -> str:
        stem = re.sub(r"[^\w\- ]+", "", self.title, flags=re.UNICODE).strip()[:80].strip()
        return f"{stem or self.key}.pdf"


def _clip(text: str | None) -> str | None:
    cleaned = strip_urls(text)
    if not cleaned:
        return None
    return cleaned if len(cleaned) <= NOTE_TEXT_LIMIT else cleaned[:NOTE_TEXT_LIMIT] + "…"


def _para(label: str, text: str | None) -> list[str]:
    clipped = _clip(text)
    return [f"<p><b>{html.escape(label)}</b>：{html.escape(clipped)}</p>"] if clipped else []


def paper_url(doi: str | None, arxiv_id: str | None) -> str | None:
    if doi:
        return f"https://doi.org/{doi}"
    if arxiv_id:
        return f"https://arxiv.org/abs/{arxiv_id}"
    return None


def reference_aliases(value: str) -> tuple[Alias, ...]:
    """用户手动给的 DOI（含 doi.org 链接）或 arXiv 编号，转成库里的标识符。"""
    candidates = (("doi", normalize_doi(value)), ("arxiv", normalize_arxiv_id(value)))
    return tuple(Alias(scheme, found) for scheme, found in candidates if found)


def build_library_entry(
    paper: Paper,
    aliases: dict[str, str],
    card: PaperCard | None,
    topic_name: str | None,
    pdf: bytes | None,
) -> LibraryEntry:
    """标签：主题名和推送等级，方便在文献库里按主题筛选。"""
    doi, arxiv_id = aliases.get("doi"), aliases.get("arxiv")
    tier_tag = (card.tier_label or card.tier) if card else None
    return LibraryEntry(
        key=paper.id,
        title=paper.title,
        authors=paper.authors,
        venue=paper.venue,
        published_at=paper.published_at,
        doi=doi,
        arxiv_id=arxiv_id,
        abstract=paper.abstract,
        url=paper_url(doi, arxiv_id),
        tags=tuple(tag for tag in (topic_name, tier_tag) if tag),
        note_html=note_html(paper.id, card, topic_name),
        pdf=pdf,
    )


def note_marker(paper_id: str) -> str:
    """写在笔记末尾。重试时据此判断笔记是否已经写过。"""
    return f"论文工厂编号：{paper_id}"


def note_html(paper_id: str, card: PaperCard | None, topic_name: str | None) -> str:
    """推荐理由写成子笔记。内容来自 LLM，全部转义，其中的链接已去掉。"""
    heading = "论文工厂推荐" + (f" · {topic_name}" if topic_name else "")
    parts = [f"<h2>{html.escape(heading)}</h2>", *_card_parts(card)]
    parts.append(f"<p>{html.escape(note_marker(paper_id))}</p>")
    return "\n".join(parts)


def _card_parts(card: PaperCard | None) -> list[str]:
    if card is None:
        return ["<p>手动加入，暂无评分。</p>"]
    label = card.tier_label or card.tier
    note = "（只读了摘要，分数是临时的）" if card.provisional else ""
    score = f"{card.score} 分 · {label}{note}"
    parts = [f"<p><b>阅读价值</b>：{html.escape(score)}</p>"]
    parts += _para("一句话总结", card.summary)
    parts += _para("创新点", card.innovation)
    parts += _para("结论", card.conclusion)
    limitations = [item for item in (_clip(text) for text in card.limitations) if item]
    if limitations:
        items = "".join(f"<li>{html.escape(item)}</li>" for item in limitations)
        parts.append(f"<p><b>不足</b>：</p><ul>{items}</ul>")
    return parts
