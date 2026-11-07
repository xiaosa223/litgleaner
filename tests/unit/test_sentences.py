"""翻译和对照显示用的分句：译文按句与原文对齐，所以切错一句整段都对不上。"""

import pytest

from litgleaner.domain.document import Block, body_start
from litgleaner.domain.sentences import split_sentences


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        (
            "We propose a model. It works well! Does it scale?",
            ["We propose a model.", "It works well!", "Does it scale?"],
        ),
        (
            "As shown in Fig. 2, the loss drops. See Eq. 3 for details.",
            ["As shown in Fig. 2, the loss drops.", "See Eq. 3 for details."],
        ),
        (
            "Prior work (e.g. RNNs, i.e. recurrent nets) is slow. We fix this.",
            ["Prior work (e.g. RNNs, i.e. recurrent nets) is slow.", "We fix this."],
        ),
        (
            "Vaswani et al. proposed it. It was 2017.",
            ["Vaswani et al. proposed it.", "It was 2017."],
        ),
        ("The BLEU is 28.4 on WMT. That is new.", ["The BLEU is 28.4 on WMT.", "That is new."]),
        ("Let $x = 1. y$ be given. Then stop.", ["Let $x = 1. y$ be given.", "Then stop."]),
        ("J. Smith wrote this. Next one.", ["J. Smith wrote this.", "Next one."]),
        (
            "近年来漏洞数目急剧增加。准确编写利用代码很关键！",
            ["近年来漏洞数目急剧增加。", "准确编写利用代码很关键！"],
        ),
        (
            "漏洞危害也引起业界广泛关注.\xa0准确、高效地编写代码.",
            ["漏洞危害也引起业界广泛关注.", "准确、高效地编写代码."],
        ),
        ("No terminal punctuation", ["No terminal punctuation"]),
        ("  ", []),
    ],
)
def test_split_sentences(text: str, expected: list[str]):
    assert split_sentences(text) == expected


def test_split_is_stable_and_keeps_all_words():
    text = "First claim. Second claim with vs. comparison. Third."
    sentences = split_sentences(text)
    assert " ".join(sentences).split() == text.split()
    assert split_sentences(text) == sentences


def test_body_starts_at_first_abstract_marker():
    blocks = (
        Block("text", "Ashish Vaswani, Google Brain", 0),
        Block("heading", "Survey of Something", 0),
        Block("text", "Abstract—We propose X.", 0),
        Block("text", "摘　要　我们提出了 X。", 0),
    )
    assert body_start(blocks) == 2
    assert body_start(blocks[3:]) == 0
    assert body_start((Block("text", "No marker here.", 0),)) == 0
