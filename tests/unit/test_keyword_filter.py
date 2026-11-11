"""关键词规则只读标题、摘要及课题查询，不依赖来源或模型。"""

import pytest

from litgleaner.domain.keyword_filter import filter_keywords
from litgleaner.domain.topic import TopicQuery


@pytest.mark.parametrize(
    ("term", "title", "abstract"),
    [
        ("GAN", "A GAN method", None),
        ("GAN", "GAN网络", None),
        ("神经网络", "一种卷积神经网络方法", None),
        ("graph neural network", "Other title", "A GRAPH\nNEURAL\tNETWORK method."),
        ("ａｌｐｈａ", "ＡＬＰＨＡ research", None),
        ("alpha beta", "alpha\u3000\tBETA", None),
        ("C++", "Using C++ for training", None),
    ],
)
def test_include_matches_normalized_words_phrases_and_chinese_substrings(term, title, abstract):
    decision = filter_keywords(title, abstract, TopicQuery(include=(term,)))
    assert decision.passed and "包含词" in decision.reason


@pytest.mark.parametrize(
    ("term", "text"),
    [
        ("GAN", "An organ study"),
        ("GAN", "A GANish model"),
        ("GAN", "prefix_GAN_suffix"),
        ("alpha", "alphabet and subalpha"),
        ("graph neural network", "A subgraph neural networks study"),
        ("C++", "A C++extra token"),
    ],
)
def test_ascii_terms_do_not_match_inside_other_words(term, text):
    decision = filter_keywords(text, None, TopicQuery(include=(term,)))
    assert not decision.passed and "未命中" in decision.reason


def test_phrase_does_not_join_title_and_abstract():
    assert not filter_keywords(
        "graph", "neural network", TopicQuery(include=("graph neural network",))
    ).passed


def test_only_synonyms_of_included_terms_are_expanded():
    query = TopicQuery(
        include=("neural network",),
        synonyms={"neural network": ("神经网络",), "other": ("ocean",)},
    )
    assert filter_keywords("神经网络研究", None, query).passed
    assert not filter_keywords("ocean", None, query).passed


def test_exclusion_has_priority_and_uses_the_same_normalization():
    decision = filter_keywords(
        "ＡＬＰＨＡ research",
        "A REVIEW\nARTICLE.",
        TopicQuery(include=("alpha",), exclude=("review article",)),
    )
    assert not decision.passed and decision.reason == "命中排除词：review article"


def test_exclusion_also_respects_ascii_boundaries_and_chinese_substrings():
    assert filter_keywords(
        "organic alpha", None, TopicQuery(include=("alpha",), exclude=("GAN",))
    ).passed
    decision = filter_keywords("卷积神经网络综述", None, TopicQuery(exclude=("综述",)))
    assert not decision.passed and "综述" in decision.reason


@pytest.mark.parametrize("query", [TopicQuery(), TopicQuery(include=(" \n",))])
def test_no_usable_include_terms_passes_conservatively(query):
    decision = filter_keywords("Anything", None, query)
    assert decision.passed and "未配置包含词" in decision.reason


def test_exclude_still_applies_when_include_is_empty():
    assert not filter_keywords("A survey", None, TopicQuery(exclude=("survey",))).passed


def test_empty_metadata_does_not_invent_a_match():
    assert not filter_keywords("", None, TopicQuery(include=("alpha",))).passed
