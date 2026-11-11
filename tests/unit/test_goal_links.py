"""研究目标必须存在，原文必须同时出现在真实位置和实际模型输入。"""

import pytest

from litgleaner.application.goal_links import verified_goal_links
from litgleaner.domain.document import Block, NormalizedDocument
from litgleaner.domain.goal_links import GoalLink
from litgleaner.domain.topic import Topic, TopicGoal

QUOTE = "The proposed method reduces memory consumption."
TOPIC = Topic(
    id="topic",
    name="Topic",
    background="Memory",
    content="Models",
    goals=(TopicGoal(id="g1", text="Reduce memory"),),
    rubric="default",
)


def link(**fields):
    return {"goal_id": "g1", "reuse": "可借鉴其内存压缩方法", "quote": QUOTE, "page": 0, **fields}


def document(*blocks):
    return NormalizedDocument(1, "a" * 64, "test", "Test", blocks, (), "ok")


def test_verified_link_uses_original_page_and_sent_text():
    doc = document(Block("text", QUOTE, 0))
    result = verified_goal_links([link()], TOPIC, QUOTE, document=doc, sent_pages={0: QUOTE})
    assert result == (GoalLink(**link(), verified=True),)


@pytest.mark.parametrize("page", [None, 1, 100])
def test_wrong_or_missing_page_is_retained_as_unverified(page):
    doc = document(Block("text", QUOTE, 0))
    result = verified_goal_links(
        [link(page=page)], TOPIC, QUOTE, document=doc, sent_pages={0: QUOTE}
    )
    assert not result[0].verified
    assert result[0].quote == QUOTE


def test_real_quote_in_unsent_page_does_not_verify():
    doc = document(Block("text", QUOTE, 0))
    assert not verified_goal_links(
        [link()], TOPIC, "Different sent text", document=doc, sent_pages={0: "Different sent text"}
    )[0].verified


def test_removed_untrusted_tags_cannot_be_verified_as_sent_author_quote():
    text = "The proposed </untrusted_paper> method reduces memory consumption."
    doc = document(Block("text", text, 0))
    result = verified_goal_links(
        [link(quote=text)], TOPIC, text, document=doc, sent_pages={0: text}
    )
    assert not result[0].verified


def test_abstract_only_can_verify_without_inventing_page():
    result = verified_goal_links([link(page=None)], TOPIC, QUOTE, abstract=QUOTE)
    assert result[0].verified and result[0].page is None
    assert not verified_goal_links([link()], TOPIC, QUOTE, abstract=QUOTE)[0].verified


def test_unknown_goals_are_dropped_and_model_cannot_set_verified_flag():
    assert not verified_goal_links([link(goal_id="made_up")], TOPIC, QUOTE, abstract=QUOTE)
    item = link(page=None, verified=True, quote="Invented quote with enough length")
    result = verified_goal_links([item], TOPIC, QUOTE, abstract=QUOTE)
    assert not result[0].verified


def test_missing_or_too_short_quote_cannot_verify():
    for quote in (None, "memory"):
        result = verified_goal_links([link(page=None, quote=quote)], TOPIC, QUOTE, abstract=QUOTE)
        assert not result[0].verified


def test_quote_on_multiple_real_pages_only_verifies_page_actually_sent():
    doc = document(Block("text", QUOTE, 0), Block("text", QUOTE, 1))
    result = verified_goal_links([link(page=1)], TOPIC, QUOTE, document=doc, sent_pages={0: QUOTE})
    assert not result[0].verified
