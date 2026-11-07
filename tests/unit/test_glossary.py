"""课题术语表：译文校验和提示词都以它为准（第 7.1、12.3 节）。"""

import pytest
from pydantic import ValidationError

from litgleaner.domain.glossary import Glossary, check_translation


def glossary() -> Glossary:
    return Glossary.model_validate(
        {
            "terms": [
                {
                    "en": ["self-attention", "intra-attention"],
                    "zh": "自注意力",
                    "definition": "序列内部各位置互相计算注意力",
                },
                {"en": "Transformer", "zh": "Transformer"},
                {"en": "BLEU", "zh": "BLEU"},
            ]
        }
    )


def test_terms_found_by_word_and_case_insensitive():
    found = glossary().terms_in("Self-attention, sometimes called intra-attention, is used.")
    assert [term.zh for term in found] == ["自注意力"]
    assert glossary().terms_in("The transformers library") == ()  # 整词匹配


def test_hash_tracks_content_not_order():
    a = glossary()
    b = Glossary(terms=tuple(reversed(a.terms)))
    assert a.glossary_hash == b.glossary_hash
    assert Glossary(terms=()).glossary_hash != a.glossary_hash


def test_merge_keeps_first_translation_for_same_term():
    other = Glossary.model_validate(
        {"terms": [{"en": "self-attention", "zh": "自我注意"}, {"en": "encoder", "zh": "编码器"}]}
    )
    merged = glossary().merged(other)
    assert [t.zh for t in merged.terms_in("self-attention encoder")] == ["自注意力", "编码器"]


def test_rejects_empty_terms():
    with pytest.raises(ValidationError):
        Glossary.model_validate({"terms": [{"en": "", "zh": "空"}]})


def test_check_translation_counts_sentences_and_required_terms():
    sources = ["Self-attention is fast.", "We report BLEU."]
    assert check_translation(sources, ["自注意力很快。", "我们报告 BLEU。"], glossary()) == ()
    problems = check_translation(sources, ["自注意力很快。"], glossary())
    assert problems and "句数" in problems[0]
    problems = check_translation(sources, ["自我注意很快。", "我们报告 BLEU。"], glossary())
    assert problems == ("第 1 句缺少术语表译名“自注意力”（self-attention）",)
