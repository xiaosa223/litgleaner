"""去重：标识符相同才自动合并，冲突不猜。"""

import pytest
from pydantic import ValidationError

from litgleaner.domain.identity import (
    Alias,
    ExistingPaper,
    decide_resolution,
    normalize_arxiv_id,
    normalize_doi,
    record_aliases,
    title_key,
)
from litgleaner.domain.paper import SourceRecord


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        ("10.1109/ABC.1", "10.1109/abc.1"),
        ("https://doi.org/10.1109/ABC.1", "10.1109/abc.1"),
        ("doi:10.1109/ABC.1 ", "10.1109/abc.1"),
        ("not-a-doi", None),
        (None, None),
    ],
)
def test_normalize_doi(value, expected):
    assert normalize_doi(value) == expected


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        ("1907.10485v1", "1907.10485"),
        ("arXiv:1907.10485", "1907.10485"),
        ("https://arxiv.org/abs/1907.10485v3", "1907.10485"),
        ("hep-th/9901001v2", "hep-th/9901001"),
        ("garbage", None),
    ],
)
def test_normalize_arxiv(value, expected):
    assert normalize_arxiv_id(value) == expected


def test_title_key_ignores_case_punctuation_and_width():
    assert title_key("Deep-Learning: A Survey!") == title_key("deep learning a survey")
    assert title_key("ＡＢＣ 研究") == title_key("abc研究")


def test_title_key_keeps_non_latin_titles_distinct():
    assert title_key("矩阵理论") != title_key("随机矩阵")


def record(**kw) -> SourceRecord:
    return SourceRecord(**{"title": "A Paper", "authors": ("Li Wei",), **kw})


def test_source_record_rejects_blank_title_and_extra_fields():
    with pytest.raises(ValidationError):
        SourceRecord(title="   ")
    with pytest.raises(ValidationError):
        SourceRecord(title="x", nonsense=1)


def test_record_aliases_normalizes_and_skips_invalid():
    rec = record(doi="https://doi.org/10.1/X", arxiv_id="1907.10485v2", external_ids={"ieee": "9"})
    assert set(record_aliases(rec)) == {
        Alias("doi", "10.1/x"),
        Alias("arxiv", "1907.10485"),
        Alias("ieee", "9"),
    }
    assert record_aliases(record(doi="bad")) == ()


def test_new_when_nothing_matches():
    decision = decide_resolution(record(), alias_hits=frozenset(), title_matches=())
    assert decision.kind == "new"


def test_alias_hit_attaches_new_aliases():
    rec = record(doi="10.1/x", arxiv_id="1907.10485")
    decision = decide_resolution(rec, alias_hits=frozenset({"P1"}), title_matches=())
    assert (decision.kind, decision.paper_id, decision.reason) == ("existing", "P1", "alias")


def test_conflicting_aliases_do_not_auto_merge():
    decision = decide_resolution(record(), alias_hits=frozenset({"P1", "P2"}), title_matches=())
    assert decision.kind == "conflict"
    assert decision.conflicting_ids == ("P1", "P2")


def test_same_title_and_first_author_merges_preprint_with_published():
    existing = ExistingPaper("P1", ("Wei Li", "Other"))
    rec = record(title="A Long Enough Paper Title", authors=("Wei Li",))
    decision = decide_resolution(rec, frozenset(), (existing,))
    assert (decision.kind, decision.paper_id, decision.reason) == ("existing", "P1", "title_author")


LONG_TITLE = "A Long Enough Paper Title About Matrices"


@pytest.mark.parametrize(
    ("first", "second", "merged"),
    [
        ("Wei Li", "Xiao Li", False),
        ("A. Smith", "B. Smith", False),
        ("A. Smith", "Alice Smith", True),
        ("Li, Wei", "Wei Li", True),
        ("Smith", "Alice Smith", False),
    ],
)
def test_first_author_match_requires_compatible_given_name(first, second, merged):
    existing = ExistingPaper("P1", (second,))
    rec = record(title=LONG_TITLE, authors=(first,))
    decision = decide_resolution(rec, frozenset(), (existing,))
    assert (decision.kind == "existing") is merged


def test_generic_short_title_is_never_merged_by_title():
    existing = ExistingPaper("P1", ("Alice Smith",))
    rec = record(title="Editorial", authors=("Alice Smith",))
    decision = decide_resolution(rec, frozenset(), (existing,))
    assert decision.kind == "new"
    assert decision.conflicting_ids == ("P1",)


def test_same_title_different_author_stays_separate_and_flagged():
    existing = ExistingPaper("P1", ("Zhang San",))
    decision = decide_resolution(record(authors=("Wei Li",)), frozenset(), (existing,))
    assert decision.kind == "new"
    assert decision.conflicting_ids == ("P1",)


def test_same_title_without_authors_is_not_high_confidence():
    existing = ExistingPaper("P1", ())
    decision = decide_resolution(record(authors=()), frozenset(), (existing,))
    assert decision.kind == "new"
    assert decision.conflicting_ids == ("P1",)
