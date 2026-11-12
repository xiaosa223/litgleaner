"""命中课题随私有卡片展示，公开白名单和渠道的文字上限保持独立。"""

import json
from dataclasses import FrozenInstanceError, asdict, replace

import pytest

from litgleaner.adapters.feishu.card import MAX_CARD_BYTES, TEXT_LIMIT, FeishuCardRenderer
from litgleaner.adapters.renderers.plain_text import PlainTextRenderer
from litgleaner.domain.digest import DigestView, MatchedTopic, PaperCard, to_public_card


def card():
    return PaperCard(
        paper_id="paper-1",
        title="A paper",
        authors=(),
        venue=None,
        published_at=None,
        doi=None,
        score=90,
        tier="must_read",
        provisional=False,
        summary=None,
        conclusion=None,
        limitations=(),
        innovation=None,
        abstract=None,
        quotes=(),
        fulltext_used=True,
        parse_quality=None,
    )


def view(item, *, visibility="private"):
    return DigestView("digest-1", "Primary topic", "2026-11-12", visibility, (item,))


@pytest.mark.parametrize("renderer", [PlainTextRenderer, FeishuCardRenderer])
def test_old_card_default_has_no_matched_topic_line(renderer):
    original = card()
    assert original.matched_topics == ()
    assert "命中课题" not in renderer().render(view(original)).body


@pytest.mark.parametrize("renderer", [PlainTextRenderer, FeishuCardRenderer])
def test_private_card_displays_all_frozen_matched_topic_names_in_order(renderer):
    original = replace(
        card(), matched_topics=(MatchedTopic("b", "课题乙"), MatchedTopic("a", "课题甲"))
    )
    body = renderer().render(view(original)).body
    assert "命中课题：课题乙、课题甲" in body
    assert original.matched_topics == (MatchedTopic("b", "课题乙"), MatchedTopic("a", "课题甲"))


def test_matched_topic_identity_and_name_are_frozen():
    match = MatchedTopic("private-id", "Original name")
    with pytest.raises(FrozenInstanceError):
        match.name = "Changed"
    assert asdict(match) == {"topic_id": "private-id", "name": "Original name"}


@pytest.mark.parametrize("renderer", [PlainTextRenderer, FeishuCardRenderer])
def test_public_projection_drops_private_topic_ids_and_names(renderer):
    original = replace(card(), matched_topics=(MatchedTopic("secret-id-7", "秘密方向七"),))
    public = to_public_card(original)
    assert "matched_topics" not in asdict(public)
    text = (
        json.dumps(asdict(public), ensure_ascii=False)
        + renderer().render(view(public, visibility="public")).body
    )
    assert "secret-id-7" not in text and "秘密方向七" not in text
    assert "命中课题" not in text


@pytest.mark.parametrize("renderer", [PlainTextRenderer, FeishuCardRenderer])
def test_public_visibility_cannot_display_topics_even_without_projection(renderer):
    original = replace(card(), matched_topics=(MatchedTopic("secret", "Hidden association"),))
    text = renderer().render(view(original, visibility="public")).body
    assert "Hidden association" not in text and "命中课题" not in text


def test_topic_markup_stays_plain_text_and_does_not_create_links_or_actions():
    name = "**bold** [click](https://evil.example) <at id=all></at>"
    original = replace(card(), matched_topics=(MatchedTopic("private", name),))
    data = json.loads(FeishuCardRenderer().render(view(original)).body)
    texts = [element["text"] for element in data["body"]["elements"] if element["tag"] == "div"]
    topic = next(text for text in texts if text["content"].startswith("命中课题"))
    assert topic == {"tag": "plain_text", "content": f"命中课题：{name}", "text_size": "normal"}
    actions = [
        column["elements"][0]["behaviors"][0]["type"]
        for element in data["body"]["elements"]
        if element["tag"] == "column_set"
        for column in element["columns"]
    ]
    assert actions == ["callback", "callback"]
    assert all(element["tag"] != "markdown" for element in data["body"]["elements"])


def test_many_topics_use_existing_message_text_limit_without_changing_snapshot():
    matches = tuple(
        MatchedTopic(f"topic-{index}", f"课题{index}" + "长" * 100) for index in range(400)
    )
    original = replace(card(), matched_topics=matches)
    rendered = FeishuCardRenderer().render(view(original))
    assert len(rendered.body.encode("utf-8")) < MAX_CARD_BYTES
    data = json.loads(rendered.body)
    line = next(
        element["text"]["content"]
        for element in data["body"]["elements"]
        if element["tag"] == "div" and element["text"]["content"].startswith("命中课题")
    )
    assert line.endswith("…") and len(line) <= len("命中课题：") + TEXT_LIMIT + 1
    assert original.matched_topics == matches and len(matches) == 400
    assert matches[-1].name in PlainTextRenderer().render(view(original)).body
