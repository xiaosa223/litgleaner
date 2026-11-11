"""调研采集只返回候选和真实引用边；来源均为内存假实现。"""

from datetime import UTC, date, datetime
from types import SimpleNamespace
from unittest.mock import Mock
from zoneinfo import ZoneInfo

import pytest

from litgleaner.application.codec import decompress
from litgleaner.application.survey_sources import collect
from litgleaner.domain.citation import ReferenceRecord
from litgleaner.domain.errors import InvalidRequest, RateLimited, SchemaChanged
from litgleaner.domain.identity import Alias
from litgleaner.domain.paper import SourceRecord
from litgleaner.domain.query import HistoricalCitations, HistoricalSearch, ReferenceList
from litgleaner.domain.survey import SurveySettings
from litgleaner.domain.survey_candidates import CandidatePool
from litgleaner.domain.topic import Topic
from litgleaner.ports.source import FetchResult


def record(title="Candidate", doi=None, **changes):
    return SourceRecord(title=title, doi=doi, **changes)


def topic(**changes):
    return Topic.model_validate(
        {
            "id": "topic",
            "name": "Topic",
            "background": "Background",
            "content": "Content",
            "goals": [{"id": "goal", "text": "A research goal"}],
            "rubric": "default",
            "query": {
                "include": ["graph"],
                "synonyms": {"graph": ["network"]},
                "exclude": ["ocean"],
            },
            **changes,
        }
    )


class FakeSource:
    def __init__(
        self, handler, capabilities=(HistoricalSearch, HistoricalCitations, ReferenceList)
    ):
        self.name = "fake"
        self.capabilities = frozenset(capabilities)
        self.handler, self.calls = handler, []

    def fetch(self, query, cursor):
        assert cursor is None
        self.calls.append(query)
        return self.handler(query)


def context(**sources):
    return SimpleNamespace(
        sources=sources,
        blobs=Mock(),
        clock=lambda: datetime(2026, 11, 11, tzinfo=UTC),
        settings=SimpleNamespace(timezone=ZoneInfo("Asia/Tokyo")),
    )


def reference(seed, doi, position=0, **changes):
    return ReferenceRecord(
        seed, position, f"Raw reference {position}", "Target", (Alias("doi", doi),), **changes
    )


def result(records=(), references=(), **changes):
    return FetchResult(tuple(records), None, references=tuple(references), **changes)


def test_history_queries_use_settings_dates_categories_terms_and_limit():
    source = FakeSource(lambda q: result([record(q.category)]), capabilities=(HistoricalSearch,))
    ctx = context(history=source)
    found = collect(ctx, topic(), SurveySettings(since=date(1999, 1, 1), category_limit=7), dois=())
    assert len(source.calls) == 3 and len(found.candidates) == 3
    assert {q.category for q in source.calls} == {"reviews", "highly_cited", "recent"}
    assert all(q.until == date(2026, 11, 11) for q in source.calls)
    assert {q.category: q.since for q in source.calls} == {
        "reviews": date(1999, 1, 1),
        "highly_cited": date(1999, 1, 1),
        "recent": date(2024, 11, 11),
    }
    assert all(
        q.limit == 7 and q.terms == ("graph", "network") and q.exclude == ("ocean",)
        for q in source.calls
    )
    assert len(found.sources) == 3 and all(
        s.source == "history" and not s.error for s in found.sources
    )


def test_second_round_only_expands_explicit_dois_and_keeps_direction():
    citing = record("Citing paper", "10.1000/citing")
    target = record("Target paper", "10.1000/target")

    def fetch(query):
        if isinstance(query, ReferenceList):
            return result(
                [target],
                [
                    reference(
                        query.dois[0],
                        "10.1000/target",
                        9,
                        contexts=("We use the earlier work.",),
                        intents=("method",),
                        influential=True,
                    )
                ],
            )
        return result([citing])

    source = FakeSource(fetch)
    found = collect(
        context(source=source),
        topic(),
        SurveySettings(),
        dois=("https://doi.org/10.1000/seed",),
        historical=False,
    )
    assert len(source.calls) == 2
    watch = next(q for q in source.calls if isinstance(q, HistoricalCitations))
    assert watch.dois == ("10.1000/seed",) and watch.since == date(1900, 1, 1)
    assert len(found.edges) == 2
    outgoing = next(edge for edge in found.edges if edge.source_position == 9)
    assert outgoing.citing_aliases == (Alias("doi", "10.1000/seed"),)
    assert outgoing.cited_aliases == (Alias("doi", "10.1000/target"),)
    assert outgoing.contexts == ("We use the earlier work.",) and outgoing.influential is True
    incoming = next(edge for edge in found.edges if edge.source_position == 0)
    assert incoming.citing_aliases == (Alias("doi", "10.1000/citing"),)
    assert incoming.cited_aliases == (Alias("doi", "10.1000/seed"),)
    assert next(c for c in found.candidates if c.record == target).referenced_by == (
        "10.1000/seed",
    )
    assert next(c for c in found.candidates if c.record == citing).referenced_by == ()


def test_dedup_preserves_source_mode_and_distinct_referencing_seeds_before_final_cap():
    target = record("Target", "https://doi.org/10.1000/shared", arxiv_id="2601.00001v2")

    def fetch(query):
        if isinstance(query, HistoricalSearch):
            return result([record("Z unrelated", "10.1000/other"), target])
        return result(
            [record("Target", arxiv_id="2601.00001")], [reference(query.dois[0], "10.1000/shared")]
        )

    source = FakeSource(fetch, (HistoricalSearch, ReferenceList))
    found = collect(
        context(one=source, two=source),
        topic(),
        SurveySettings(candidate_limit=1),
        dois=("10.1000/b", "10.1000/a", "DOI:10.1000/a"),
    )
    assert len(found.candidates) == 1
    candidate = found.candidates[0]
    assert candidate.key == "doi:10.1000/shared"
    assert candidate.sources == ("one", "two")
    assert candidate.modes == ("highly_cited", "recent", "references", "reviews")
    assert candidate.referenced_by == ("10.1000/a", "10.1000/b")
    assert any("candidate_limit" in gap for gap in found.uncovered)


def test_failures_are_isolated_by_query_and_only_error_type_is_exposed():
    def fetch(query):
        if query.category == "reviews":
            raise RateLimited("a secret error detail")
        if query.category == "highly_cited":
            raise RuntimeError("unexpected private error")
        return result(
            [record("Recent")], warnings=("Actual warning",), truncated=True, raw=b"raw response"
        )

    ctx = context(test=FakeSource(fetch, (HistoricalSearch,)))
    found = collect(ctx, topic(), SurveySettings(), dois=())
    assert [s.error for s in found.sources] == ["RateLimited", "RuntimeError", None]
    assert len(found.candidates) == 1 and found.sources[-1].truncated
    assert found.sources[-1].warnings == ("Actual warning",)
    assert found.sources[-1].raw_key
    key, payload = ctx.blobs.put.call_args.args
    assert key == found.sources[-1].raw_key and decompress(payload) == b"raw response"
    assert "secret" not in repr(found) and "private" not in repr(found)


def test_unavailable_capabilities_and_empty_keywords_are_reported_without_fake_watch():
    ordinary = FakeSource(lambda q: (_ for _ in ()).throw(AssertionError("must not call")), ())
    found = collect(
        context(ordinary=ordinary), topic(query={"include": []}), SurveySettings(), dois=()
    )
    assert not found.candidates and not found.sources and found.uncovered
    assert not ordinary.calls


def test_per_query_caps_do_not_allow_tail_records_or_cross_parent_edges():
    seed = "10.1000/seed"
    selected, tail = record("Selected", "10.1000/selected"), record("Tail", "10.1000/tail")

    def fetch(query):
        if isinstance(query, ReferenceList):
            return result(
                [tail, selected], [reference(seed, selected.doi), reference(seed, tail.doi, 1)]
            )
        return result([selected, tail])

    source = FakeSource(fetch, (HistoricalCitations, ReferenceList))
    found = collect(
        context(source=source),
        topic(),
        SurveySettings(expansion_limit=1),
        dois=(seed,),
        historical=False,
    )
    assert {c.record.title for c in found.candidates} == {"Selected"}
    assert all(s.truncated for s in found.sources)
    assert all("expansion_limit" in " ".join(s.warnings) for s in found.sources)
    assert len(found.edges) == 2 and all(
        Alias("doi", tail.doi) not in e.cited_aliases for e in found.edges
    )

    wrong = FakeSource(
        lambda q: result([selected], [reference("10.1000/other", selected.doi)]), (ReferenceList,)
    )
    failed = collect(
        context(source=wrong), topic(), SurveySettings(), dois=(seed,), historical=False
    )
    assert (
        not failed.edges
        and not failed.candidates
        and failed.sources[0].error == SchemaChanged.__name__
    )


def test_future_start_rejected_before_any_source_call_and_seed_limit_is_reported():
    source = FakeSource(lambda q: result(), (ReferenceList,))
    with pytest.raises(InvalidRequest):
        collect(context(source=source), topic(), SurveySettings(since=date(2099, 1, 1)), dois=())
    assert not source.calls
    found = collect(
        context(source=source),
        topic(),
        SurveySettings(seed_limit=1),
        dois=("bad", "10.1000/b", "10.1000/a"),
        historical=False,
    )
    assert len(source.calls) == 1 and source.calls[0].dois == ("10.1000/a",)
    assert any("seed_limit" in gap for gap in found.uncovered)
    assert any("DOI" in gap for gap in found.uncovered)


def test_identifierless_reference_title_can_supply_candidate_without_inventing_alias():
    target = record("A title without identifiers")
    source = FakeSource(
        lambda q: result(
            [target],
            [ReferenceRecord(q.dois[0], 3, "Original reference", target.title, ())],
        ),
        (ReferenceList,),
    )
    found = collect(
        context(test=source), topic(), SurveySettings(), dois=("10.1000/seed",), historical=False
    )
    assert len(found.candidates) == 1
    assert found.candidates[0].referenced_by == ("10.1000/seed",)
    assert found.edges[0].cited_aliases == ()
    assert found.candidates[0].key.startswith("title:")


def test_candidate_pool_uses_exact_ids_then_unambiguous_title_and_preserves_zero_count():
    pool = CandidatePool()
    pool.add(record("Same title", "10.1000/a", citation_count=0), "one", "reviews")
    pool.add(record("Same title", "10.1000/b"), "two", "recent")
    pool.add(record("Same title"), "three", "recent")
    assert len(pool.candidates()) == 3  # two different DOI facts cannot be merged by title
    pool = CandidatePool()
    pool.add(
        record("A sufficiently LONG shared title", authors=("Alice Smith",), citation_count=0),
        "one",
        "reviews",
    )
    pool.add(
        record(
            "A sufficiently long shared title",
            "10.1000/a",
            authors=("A. Smith",),
            citation_count=50,
        ),
        "two",
        "recent",
    )
    candidates = pool.candidates()
    assert len(candidates) == 1 and candidates[0].key == "doi:10.1000/a"
    assert candidates[0].record.citation_count == 0
    assert candidates[0].sources == ("one", "two")


@pytest.mark.parametrize(
    "title, first, second",
    [
        ("Editorial", "Alice Smith", "Alice Smith"),
        ("A sufficiently long shared title", "Alice Smith", "Bob Jones"),
        ("A sufficiently long shared title", None, "Alice Smith"),
    ],
)
def test_title_fallback_never_merges_short_or_different_author_facts(title, first, second):
    pool = CandidatePool()
    pool.add(record(title, authors=(first,) if first else ()), "one", "reviews")
    pool.add(record(title, "10.1000/other", authors=(second,) if second else ()), "two", "recent")
    assert len(pool.candidates()) == 2
    assert len({c.key for c in pool.candidates()}) == 2
    assert (
        pool.candidates()[0].record.authors != pool.candidates()[1].record.authors
        or title == "Editorial"
    )


def test_recent_uses_two_calendar_years_with_leap_day_and_respects_later_start():
    source = FakeSource(lambda q: result(), (HistoricalSearch,))
    ctx = context(source=source)
    ctx.clock = lambda: datetime(2024, 2, 29, 12, tzinfo=UTC)
    collect(ctx, topic(), SurveySettings(), dois=())
    recent = next(q for q in source.calls if q.category == "recent")
    assert recent.since == date(2022, 2, 28) and recent.until == date(2024, 2, 29)
    source.calls.clear()
    collect(ctx, topic(), SurveySettings(since=date(2023, 1, 1)), dois=())
    assert all(q.since == date(2023, 1, 1) for q in source.calls)


def test_raw_archive_failure_is_isolated_from_other_sources_and_has_no_private_details():
    broken = FakeSource(
        lambda q: result([record("Unarchived")], raw=b"broken"), (HistoricalSearch,)
    )
    working = FakeSource(lambda q: result([record("Available")]), (HistoricalSearch,))
    ctx = context(a=broken, b=working)
    ctx.blobs.put.side_effect = OSError("private path must not escape")
    found = collect(ctx, topic(), SurveySettings(), dois=())
    assert {c.record.title for c in found.candidates} == {"Available"}
    assert [s.error for s in found.sources] == ["OSError", None] * 3
    assert "private path" not in repr(found)


def test_history_uses_local_date_without_writing_papers_or_jobs():
    source = FakeSource(lambda q: result(), (HistoricalSearch,))
    ctx = context(source=source)
    ctx.clock = lambda: datetime(2026, 11, 11, 23, 30, tzinfo=UTC)
    ctx.store = Mock()
    collect(ctx, topic(), SurveySettings(), dois=())
    assert all(q.until == date(2026, 11, 12) for q in source.calls)
    ctx.store.assert_not_called()
    assert not ctx.store.mock_calls


def test_identifier_bridge_conflict_preserves_three_candidates_instead_of_mixing_facts():
    pool = CandidatePool()
    pool.add(record("First", "10.1000/first", authors=("Alice Smith",)), "one", "reviews")
    pool.add(record("Second", arxiv_id="2601.00001", authors=("Bob Jones",)), "two", "recent")
    pool.add(record("Bridge", "10.1000/first", arxiv_id="2601.00001"), "three", "recent")
    candidates = pool.candidates()
    assert len(candidates) == 3 and len({c.key for c in candidates}) == 3
    assert {c.record.title: c.record.authors for c in candidates} == {
        "First": ("Alice Smith",),
        "Second": ("Bob Jones",),
        "Bridge": (),
    }
