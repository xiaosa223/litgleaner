"""检索式草稿的清理规则：去重不分大小写，空白丢弃，排除词不能和检索词重叠。"""

import pytest

from litgleaner.domain.topic_draft import build_query, draft_from_answer

FIELDS = {
    "topic_id": "t", "plan_hash": "p", "profile_hash": "h", "model": "m",
    "created_at": "2026-11-13T00:00:00Z", "reading_language": "zh-CN",
}  # fmt: skip


def test_build_query_dedupes_terms_and_synonyms_case_insensitively():
    query = build_query(
        [("EEG  model", ["eeg model", "Brain model"]), ("brain MODEL", []), ("x", [" "])],
        ["fMRI", "fmri"],
    )
    assert query.include == ("EEG model", "x")
    assert dict(query.synonyms) == {"EEG model": ("Brain model",)}
    assert query.exclude == ("fMRI",)


@pytest.mark.parametrize(
    ("include", "exclude"),
    [([], []), ([(" ", [])], []), ([("a", ["b"])], ["B"])],
)
def test_build_query_rejects_empty_or_self_excluding_queries(include, exclude):
    with pytest.raises(ValueError):
        build_query(include, exclude)


def test_draft_from_answer_drops_duplicates_and_conflicting_exclusions_with_a_warning():
    draft = draft_from_answer(
        {
            "include": [
                {"term": "EEG", "reason": "r", "synonyms": ["eeg", "electroencephalography"]},
                {"term": "eeg", "reason": "dup", "synonyms": []},
            ],
            "exclude": [{"term": "Electroencephalography", "reason": "r"}],
            "glossary": [{"term": "EEG", "translation": "脑电图", "definition": "d"}],
        },
        **FIELDS,
    )
    assert [(t.term, t.synonyms) for t in draft.include] == [("EEG", ("electroencephalography",))]
    assert draft.exclude == () and draft.warnings
    assert draft.glossary[0].forms == ("EEG",)
