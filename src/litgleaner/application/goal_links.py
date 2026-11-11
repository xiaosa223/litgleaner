"""筛掉不存在的目标，并在原页和实际发送的内容上同时核验推荐理由。"""

from collections.abc import Iterable, Mapping
from typing import Any

from litgleaner.domain.document import NormalizedDocument
from litgleaner.domain.evidence import clean_untrusted, verify_quote
from litgleaner.domain.goal_links import GoalLink
from litgleaner.domain.topic import Topic


def verified_goal_links(
    items: Iterable[Mapping[str, Any]],
    topic: Topic,
    sent_text: str,
    *,
    document: NormalizedDocument | None = None,
    abstract: str | None = None,
    sent_pages: Mapping[int, str] | None = None,
) -> tuple[GoalLink, ...]:
    valid_goals = {goal.id for goal in topic.goals}
    sent = clean_untrusted(sent_text)
    result: list[GoalLink] = []
    for item in items:
        link = GoalLink.model_validate({**item, "verified": False})
        if link.goal_id not in valid_goals:
            continue
        original = _original(document, abstract, link.page)
        page_text = (sent_pages or {}).get(link.page, "") if link.page is not None else ""
        page_sent = sent if document is None else clean_untrusted(page_text)
        verified = (
            verify_quote(link.quote, original)
            and verify_quote(link.quote, sent)
            and verify_quote(link.quote, page_sent)
        )
        result.append(link.model_copy(update={"verified": verified}))
    return tuple(result)


def _original(document: NormalizedDocument | None, abstract: str | None, page: int | None) -> str:
    if document is None:
        return (abstract or "") if page is None else ""
    if page is None:
        return ""
    return "\n".join(
        text
        for block in document.blocks
        if block.page == page
        for text in (block.caption, block.text)
        if text
    )
