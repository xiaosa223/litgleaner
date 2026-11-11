"""批量路线快照：版本选择、引文归属与候选数量无关的 SQL 上限。"""

import sqlite3
from dataclasses import replace

import pytest
from sqlalchemy import event, text

from litgleaner.domain.citation import Citation
from litgleaner.domain.goal_links import GoalLink
from litgleaner.domain.identity import Alias
from litgleaner.domain.paper import Paper
from litgleaner.domain.rubric import ScoreResult
from litgleaner.infra.db.migrate import upgrade_database
from litgleaner.infra.db.store import SqlStore
from litgleaner.ports.store import FullTextState, NewAssessment, TriageResult


@pytest.fixture
def store(tmp_path):
    url = f"sqlite:///{tmp_path / 'route.db'}"
    upgrade_database(url)
    result = SqlStore(url)
    yield result
    result.dispose()


def _paper(store, paper_id, **facts):
    item = Paper(paper_id, paper_id, paper_id, None, (), None, None, None, "t", **facts)
    store.papers.insert(item)
    return item


def _triage(store, paper_id, *, topic="topic", topic_hash="current", relevant=True):
    store.judgments.save_triage(
        TriageResult(paper_id, topic, topic_hash, relevant, "reason", "v", "model"), "t"
    )


def _assessment(store, paper_id, *, input_hash="input", **changes):
    item = NewAssessment(
        paper_id,
        "topic",
        "current",
        "semantic",
        input_hash,
        {"value": 5},
        {},
        True,
        "model",
        "v",
        (GoalLink(goal_id="g", reuse="An original recommendation"),),
    )
    store.judgments.insert_assessment(replace(item, **changes), "t")
    return store._db.fetch(text("SELECT max(id) FROM assessment"))[0][0]


def _score(store, assessment_id, *, policy="policy", value=70, provisional=False):
    store.judgments.save_score(
        assessment_id, policy, ScoreResult(value, "must_read", provisional, False), "t"
    )


def _cite(store, citing, target, position, *, aliases=(), source="api"):
    item = Citation(
        citing,
        position,
        "Original reference",
        source,
        0,
        target,
        None,
        aliases,
        (),
        (),
        None,
        "t",
    )
    assert store.citations.add(item)
    return item


def _snapshot(store, **scope):
    return store.reading_routes.snapshot(
        scope.get("topic_id", "topic"),
        scope.get("topic_hash", "current"),
        scope.get("semantic_hash", "semantic"),
        scope.get("policy_hash", "policy"),
    )


def test_candidates_include_unscored_relevant_papers_only_at_current_topic_hash(store):
    expected = _paper(store, "a", publication_types=("Review",), citation_count=100_000)
    _paper(store, "b")
    _triage(store, "b")
    _triage(store, "a")
    for paper_id, changes in (
        ("old", {"topic_hash": "old"}),
        ("other", {"topic": "other"}),
        ("rejected", {"relevant": False}),
    ):
        _paper(store, paper_id)
        _triage(store, paper_id, **changes)
    _paper(store, "untriaged")
    loaded = _snapshot(store)
    assert loaded.papers == (expected, store.papers.get("b"))
    assert loaded.scores == loaded.fulltexts == loaded.citations == {}
    assert _snapshot(store, topic_hash="unknown").papers == ()
    assert _snapshot(store, topic_id="topic' OR 1=1 --").papers == ()


def test_latest_matching_assessment_never_falls_back_to_older_scored_or_other_policy(store):
    for paper_id in ("unscored", "wrong-policy", "current", "wrong-scope"):
        _paper(store, paper_id)
        _triage(store, paper_id)
    old = _assessment(store, "unscored", input_hash="old-input")
    _score(store, old, value=100)
    _assessment(store, "unscored", input_hash="new-input")
    old_policy = _assessment(store, "wrong-policy")
    _score(store, old_policy, policy="old-policy")
    current = _assessment(store, "current")
    _score(store, current, value=0, provisional=True)
    wrong = _assessment(store, "current", topic_hash="old", input_hash="old-topic")
    _score(store, wrong, value=100)
    different = _assessment(store, "current", semantic_hash="other-semantic")
    _score(store, different, value=99)
    wrong_topic = _assessment(store, "wrong-scope", topic_id="other")
    _score(store, wrong_topic)
    loaded = _snapshot(store)
    assert set(loaded.scores) == {"current"}
    found = loaded.scores["current"]
    assert found == store.judgments.scored_for_paper("current")[0]
    assert found.assessment.id == current and found.score == 0 and found.provisional is True
    assert found.assessment.goal_links[0].goal_id == "g"
    assert _snapshot(store, policy_hash="missing").scores == {}


def test_fulltext_is_typed_and_scoped_without_dropping_candidates_that_have_no_state(store):
    for paper_id in ("ready", "missing", "outside"):
        _paper(store, paper_id)
    _triage(store, "ready")
    _triage(store, "missing")
    state = FullTextState("ready", "parsed", "sha", "doc/v1.json", "provider", "license", "good")
    store.papers.save_fulltext(state, "t")
    store.papers.save_fulltext(replace(state, paper_id="outside"), "t")
    loaded = _snapshot(store)
    assert loaded.fulltexts == {"ready": state}
    assert {item.id for item in loaded.papers} == {"ready", "missing"}


def _citation_graph(store):
    for paper_id in ("target", "peer", "citer1", "citer2", "citer3", "outsider", "old", "rejected"):
        _paper(store, paper_id)
        if paper_id not in {"outsider", "old", "rejected"}:
            _triage(store, paper_id)
    _triage(store, "outsider", topic="other")
    _triage(store, "old", topic_hash="old")
    _triage(store, "rejected", relevant=False)
    aliases = (Alias("doi", "10.1000/target"), Alias("arxiv", "2601.00001"))
    store.papers.add_aliases("target", aliases)
    store.papers.add_aliases("peer", (Alias("doi", "10.1000/peer"),))
    store.papers.add_aliases("outsider", (Alias("doi", "10.1000/outsider"),))
    _cite(store, "citer1", "target", "api:0", source="a")
    _cite(store, "citer1", "target", "api:1", source="b")
    _cite(store, "citer2", None, "api:0", aliases=(*aliases, aliases[0], Alias("doi", "unknown")))
    for citing in ("outsider", "old", "rejected", "target"):
        _cite(store, citing, "target", "api:0")
    _cite(store, "peer", "peer", "api:0")
    ambiguous = (Alias("doi", "10.1000/peer"), Alias("doi", "10.1000/outsider"))
    _cite(store, "citer1", None, "api:ambiguous", aliases=ambiguous)
    _cite(store, "citer2", "target", "api:fixed", aliases=ambiguous)
    _cite(store, "citer3", None, "api:case", aliases=(Alias("DOI", "10.1000/peer"),))


def test_bulk_counts_equal_incoming_with_global_alias_ambiguity_and_distinct_nonself_citing(store):
    _citation_graph(store)
    loaded = _snapshot(store)
    relevant = {item.id for item in loaded.papers}
    expected = {}
    for paper_id in relevant:
        citing = {
            item.citing_paper_id
            for item in store.citations.incoming(paper_id)
            if item.citing_paper_id in relevant and item.citing_paper_id != paper_id
        }
        if citing:
            expected[paper_id] = len(citing)
    assert loaded.citations == expected == {"target": 2}
    assert store.citations.outgoing("citer2")[0].cited_paper_id is None


def test_exact_alias_arriving_later_changes_only_snapshot_link_without_rewriting_fact(store):
    for paper_id in ("target", "citer"):
        _paper(store, paper_id)
        _triage(store, paper_id)
    alias = Alias("doi", "10.1000/later")
    original = _cite(store, "citer", None, "api:0", aliases=(alias,))
    assert _snapshot(store).citations == {}
    store.papers.add_aliases("target", (alias,))
    assert _snapshot(store).citations == {"target": 1}
    assert store.citations.outgoing("citer") == (original,)


def _seed_many(store, count):
    with store.atomic():
        for index in range(count):
            paper_id = f"P{index:04}"
            _paper(store, paper_id)
            _triage(store, paper_id)
        store._db.write(
            text(
                "INSERT INTO fulltext(paper_id,status,doc_key,updated_at) "
                "SELECT id,'parsed','doc-v1','t' FROM paper"
            )
        )
        store._db.write(
            text(
                "INSERT INTO assessment(paper_id,topic_id,topic_hash,semantic_hash,input_hash,"
                "levels,evidence,fulltext_used,model,prompt_version,created_at) "
                "SELECT id,'topic','current','semantic','input','{}','{}',1,'m','v','t' FROM paper"
            )
        )
        store._db.write(
            text(
                "INSERT INTO score(assessment_id,policy_hash,score,tier,provisional,created_at) "
                "SELECT id,'policy',80,'must_read',0,'t' FROM assessment"
            )
        )
        store._db.write(
            text(
                "INSERT INTO citation(citing_paper_id,position,raw_text,source,source_position,"
                "cited_paper_id,cited_aliases,contexts,intents,created_at) "
                "SELECT id,'api:0','Reference','api',0,'P0000','[]','[]','[]','t' FROM paper"
            )
        )


@pytest.mark.parametrize("count", [10, 1000])
def test_snapshot_uses_four_queries_independent_of_candidate_count_and_sql_variable_limit(
    store, count
):
    _seed_many(store, count)
    statements = []
    engine = store._db._engine

    def record(_conn, _cursor, statement, parameters, _context, _executemany):
        statements.append((statement, len(parameters)))

    with engine.connect() as connection:
        raw = connection.connection.driver_connection
        previous = raw.setlimit(sqlite3.SQLITE_LIMIT_VARIABLE_NUMBER, 999)
    event.listen(engine, "before_cursor_execute", record)
    try:
        loaded = _snapshot(store)
    finally:
        event.remove(engine, "before_cursor_execute", record)
        with engine.connect() as connection:
            connection.connection.driver_connection.setlimit(
                sqlite3.SQLITE_LIMIT_VARIABLE_NUMBER, previous
            )
    assert len(loaded.papers) == len(loaded.scores) == len(loaded.fulltexts) == count
    assert loaded.citations == {"P0000": count - 1}
    assert len(statements) == 4 and max(bound for _, bound in statements) <= 6
