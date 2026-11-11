"""开放引用格式只保存来源事实，缺失或有歧义的标识符不猜测。"""

import json
import re
from dataclasses import replace

import pytest

from litgleaner.domain.export_references import render_references
from litgleaner.domain.identity import Alias
from litgleaner.domain.paper import Paper


def paper(id_: str = "paper-1", **changes) -> Paper:
    item = Paper(
        id_,
        "An Original Title",
        "anoriginaltitle",
        None,
        ("Wei Li", "Smith, Alice"),
        "Original Venue",
        None,
        "2025-03-14",
        "2026-11-11T00:00:00+00:00",
    )
    return replace(item, **changes)


def assert_balanced_records(text: str, count: int) -> None:
    depth = 0
    starts = 0
    for line in text.splitlines():
        if line.startswith("@"):
            assert depth == 0
            starts += 1
        for char in line:
            depth += (char == "{") - (char == "}")
            assert depth >= 0
    assert depth == 0 and starts == count


def test_empty_export_is_valid_empty_csl_and_bibtex():
    result = render_references(())
    assert json.loads(result.csl_json) == []
    assert result.bibtex == ""
    assert result.unverified_ids == result.warnings == ()


def test_missing_or_invalid_identifiers_are_separately_reported():
    result = render_references(
        (
            (paper("b"), (Alias("doi", "not-a-doi"), Alias("arxiv", "not-an-id"))),
            (paper("a"), (Alias("other", "10.1234/looks-like-doi"),)),
        )
    )
    assert result.unverified_ids == ("a", "b")
    assert json.loads(result.csl_json) == [] and result.bibtex == ""
    assert any("b" in warning for warning in result.warnings)


def test_doi_and_arxiv_are_normalized_and_both_preserved():
    item = paper()
    result = render_references(
        (
            (
                item,
                (
                    Alias("doi", "https://doi.org/10.1234/ABC"),
                    Alias("arxiv", "https://arxiv.org/abs/2401.12345v3"),
                ),
            ),
        )
    )
    data = json.loads(result.csl_json)
    assert data == [
        {
            "id": item.id,
            "type": "article",
            "title": item.title,
            "author": [{"literal": name} for name in item.authors],
            "container-title": item.venue,
            "issued": {"date-parts": [[2025, 3, 14]]},
            "DOI": "10.1234/abc",
            "URL": "https://arxiv.org/abs/2401.12345",
        }
    ]
    assert "doi = {10.1234/abc}" in result.bibtex
    assert "eprint = {2401.12345}" in result.bibtex
    assert "archivePrefix = {arXiv}" in result.bibtex
    assert result.bibtex.startswith("@misc{litgleaner-")
    assert_balanced_records(result.bibtex, 1)
    assert result.unverified_ids == result.warnings == ()


def test_old_style_arxiv_without_date_or_authors_does_not_invent_them():
    item = paper(authors=(), published_at=None, venue=None)
    result = render_references(((item, (Alias("arxiv", "hep-th/9901001v2"),)),))
    data = json.loads(result.csl_json)[0]
    assert data["URL"] == "https://arxiv.org/abs/hep-th/9901001"
    assert not {"author", "issued", "container-title", "DOI"}.intersection(data)
    assert "year =" not in result.bibtex and "author =" not in result.bibtex


def test_hostile_metadata_cannot_close_fields_or_inject_entries():
    title = "Unmatched } title\r\n@article{injected, title={bad}} % # & _ $ ^ ~ \\"
    names = ("Research and Development", "} Evil\n@misc{bad", "Li, Wei")
    item = paper("../../unsafe,}\n@misc{bad", title=title, authors=names, venue="{Venue%")
    result = render_references(((item, (Alias("doi", "10.1234/safe"),)),))
    data = json.loads(result.csl_json)[0]
    assert data["title"] == title
    assert data["author"] == [{"literal": name} for name in names]
    assert data["container-title"] == "{Venue%"
    assert_balanced_records(result.bibtex, 1)
    assert re.match(r"@misc\{litgleaner-[a-f0-9]{64},\n", result.bibtex)
    assert r"\textbraceright{}" in result.bibtex
    assert r"\textbraceleft{}" in result.bibtex
    assert r"\textbackslash{}" in result.bibtex
    for char in ("%", "#", "&", "_", "$"):
        assert "\\" + char in result.bibtex
    assert "{Research and Development} and " in result.bibtex


def test_identifier_punctuation_is_safe_for_bibtex_and_url():
    doi = "10.1234/a}?x#part"
    result = render_references(((paper(), (Alias("doi", doi),)),))
    data = json.loads(result.csl_json)[0]
    assert data["DOI"] == doi
    assert data["URL"] == "https://doi.org/10.1234/a%7D%3Fx%23part"
    assert_balanced_records(result.bibtex, 1)


def test_issn_and_precise_date_are_preserved_without_inventing_journal_type():
    result = render_references(((paper(issn="1234-5678"), (Alias("doi", "10.1234/a"),)),))
    data = json.loads(result.csl_json)[0]
    assert data["ISSN"] == "1234-5678" and data["type"] == "article"
    assert "issn = {1234-5678}" in result.bibtex
    assert "date = {2025-03-14}" in result.bibtex
    assert "journal =" not in result.bibtex


def test_control_bytes_cannot_damage_bibtex_or_enter_identifiers():
    item = paper(title="Original\x00Control\x1bTitle")
    result = render_references(
        ((item, (Alias("doi", "10.1234/a\x00"), Alias("arxiv", "2401.12345"))),)
    )
    assert all(ord(char) >= 32 or char == "\n" for char in result.bibtex)
    data = json.loads(result.csl_json)[0]
    assert data["title"] == item.title and "DOI" not in data
    assert result.warnings and result.unverified_ids == ()


@pytest.mark.parametrize("date", ["not-a-date", "2025", "2025-02-29", "2025-13-01"])
def test_invalid_or_incomplete_dates_remain_unknown(date):
    result = render_references(((paper(published_at=date), (Alias("doi", "10.1234/a"),)),))
    assert "issued" not in json.loads(result.csl_json)[0]
    assert "year =" not in result.bibtex
    assert any("paper-1" in warning for warning in result.warnings)


def test_duplicate_inputs_merge_aliases_and_are_order_independent():
    first = (paper("b"), (Alias("doi", "10.1234/B"),))
    second = (paper("a"), (Alias("arxiv", "2401.12345v1"),))
    third = (paper("b"), (Alias("arxiv", "2501.12345"), Alias("doi", "10.1234/b")))
    result = render_references((first, second, third, first))
    assert result == render_references((third, second, first))
    assert [record["id"] for record in json.loads(result.csl_json)] == ["a", "b"]
    assert_balanced_records(result.bibtex, 2)


def test_duplicate_id_with_conflicting_metadata_is_rejected():
    aliases = (Alias("doi", "10.1234/a"),)
    with pytest.raises(ValueError, match="paper_metadata_conflict"):
        render_references(((paper(), aliases), (paper(title="Different Title"), aliases)))


def test_conflicting_identifiers_are_not_selected_arbitrarily():
    aliases = (Alias("doi", "10.1234/a"), Alias("doi", "10.1234/b"))
    result = render_references(((paper(), aliases),))
    assert result.unverified_ids == ("paper-1",)
    assert json.loads(result.csl_json) == []
    assert any("doi" in warning for warning in result.warnings)


def test_unambiguous_other_identifier_can_be_used_despite_conflict():
    aliases = (Alias("doi", "10.1234/a"), Alias("doi", "10.1234/b"), Alias("arxiv", "2401.12345"))
    result = render_references(((paper(), aliases),))
    data = json.loads(result.csl_json)[0]
    assert "DOI" not in data and data["URL"].endswith("2401.12345")
    assert result.unverified_ids == () and result.warnings


@pytest.mark.parametrize(
    ("source_type", "csl_type", "bib_type", "venue_field"),
    [
        ("book", "book", "book", "howpublished"),
        (" Ｂｏｏｋ ", "book", "book", "howpublished"),
        ("book-chapter", "chapter", "incollection", "booktitle"),
        ("BookChapter", "chapter", "incollection", "booktitle"),
        ("book_section", "chapter", "incollection", "booktitle"),
        ("chapter", "chapter", "incollection", "booktitle"),
        ("journal-article", "article-journal", "article", "journal"),
        ("JournalArticle", "article-journal", "article", "journal"),
        ("proceedings-article", "paper-conference", "inproceedings", "booktitle"),
        ("conference-paper", "paper-conference", "inproceedings", "booktitle"),
        ("Conference", "paper-conference", "inproceedings", "booktitle"),
        ("article", "article", "misc", "howpublished"),
        ("review", "article", "misc", "howpublished"),
    ],
)
def test_explicit_source_publication_type_is_preserved(
    source_type, csl_type, bib_type, venue_field
):
    item = paper(publication_types=(source_type,))
    result = render_references(((item, (Alias("doi", "10.1234/a"),)),))
    assert json.loads(result.csl_json)[0]["type"] == csl_type
    assert result.bibtex.startswith(f"@{bib_type}{{")
    assert f"{venue_field} = {{Original Venue}}" in result.bibtex
    assert "publisher =" not in result.bibtex and "volume =" not in result.bibtex
    assert "pages =" not in result.bibtex and result.warnings == ()


@pytest.mark.parametrize("types", [("book", "book-chapter"), ("JournalArticle", "Conference")])
def test_conflicting_specific_publication_types_fall_back_without_selecting_one(types):
    item = paper(publication_types=types)
    result = render_references(((item, (Alias("doi", "10.1234/a"),)),))
    assert json.loads(result.csl_json)[0]["type"] == "article"
    assert result.bibtex.startswith("@misc{")
    assert result.unverified_ids == ()
    assert any("paper-1" in warning and "类型" in warning for warning in result.warnings)
    reverse = replace(item, publication_types=tuple(reversed(types)))
    assert render_references(((reverse, (Alias("doi", "10.1234/a"),)),)) == result


def test_unknown_publication_type_does_not_use_title_or_venue_to_guess():
    item = paper(
        title="Journal Article Chapter",
        venue="Proceedings of a Conference",
        publication_types=("unknown-future-type",),
    )
    result = render_references(((item, (Alias("doi", "10.1234/a"),)),))
    assert json.loads(result.csl_json)[0]["type"] == "article"
    assert result.bibtex.startswith("@misc{") and result.warnings == ()


def test_generic_review_tag_does_not_override_specific_book_type():
    item = paper(publication_types=("review", "Book", "book"))
    result = render_references(((item, (Alias("doi", "10.1234/a"),)),))
    assert json.loads(result.csl_json)[0]["type"] == "book"
    assert result.bibtex.startswith("@book{") and result.warnings == ()
