"""个人数据快照覆盖全库和全部历史，不写数据库或读取 PDF。"""

import sqlite3
from concurrent.futures import ThreadPoolExecutor
from threading import Event

import pytest
from sqlalchemy import event

from litgleaner.domain.goal_links import GoalLink
from litgleaner.domain.identity import Alias
from litgleaner.domain.knowledge import ConceptOccurrence
from litgleaner.domain.paper import Paper
from litgleaner.domain.qa import Answer, Citation, Question, Statement
from litgleaner.domain.reading import NewAnnotation
from litgleaner.domain.rubric import ScoreResult
from litgleaner.infra.db.migrate import upgrade_database
from litgleaner.infra.db.schema import assessment
from litgleaner.infra.db.store import SqlStore
from litgleaner.ports.export import ExportSnapshot
from litgleaner.ports.store import FullTextState, NewAssessment


@pytest.fixture
def store(tmp_path):
    url = f"sqlite:///{tmp_path / 'export.db'}"
    upgrade_database(url)
    store = SqlStore(url)
    yield store
    store.dispose()


def paper(id_, *, first_seen="2000-01-01"):
    return Paper(
        id_,
        f"原始标题 {id_}",
        id_.lower(),
        None,
        ("A. Author", "B. Author"),
        "Original venue",
        "1234-5678",
        "1999-01-01",
        first_seen,
        ("Article",),
        7,
    )


def question(paper_id, index, created="t1"):
    return Question(
        f"{paper_id}-q-{index:03}",
        paper_id,
        f"request-{index}",
        f"问题 {index}",
        "作者原话",
        0,
        "zh-CN",
        created,
    )


def answer(item, created="t2"):
    return Answer(
        item.id.replace("-q-", "-a-"),
        item.paper_id,
        item.id,
        (
            Statement("模型推断", (), True),
            Statement(
                "模型解释",
                (Citation(item.paper_id, 4, "作者原文引用"),),
                False,
            ),
        ),
        "test-model",
        "producer-v1",
        f"input-{item.id}",
        created,
    )


def test_empty_database_exports_empty_snapshot(store):
    assert store.export_snapshot() == ExportSnapshot((), ())


def test_all_papers_export_even_without_library_membership_or_recent_activity(store):
    originals = (paper("Z"), paper("A", first_seen="2026-11-11"), paper("M"))
    for item in originals:
        store.papers.insert(item)
    exported = store.export_snapshot().papers
    assert tuple(item.paper for item in exported) == tuple(sorted(originals, key=lambda p: p.id))
    for item in exported:
        assert item.aliases == item.annotations == item.extractions == item.assessments == ()
        assert item.questions == item.answers == ()
        assert item.reading is None and item.pdf_sha256 is None


def test_paper_metadata_aliases_reading_annotations_and_pdf_are_isolated(store):
    for id_ in ("B", "A", "C"):
        store.papers.insert(paper(id_))
    store.papers.add_aliases("A", (Alias("z", "last"), Alias("a", "second"), Alias("a", "first")))
    store.papers.add_aliases("B", (Alias("b", "only-B"),))
    store.reader.set_state("A", "reading", {"view": "pdf", "page": 0}, "t0")
    note = store.reader.add_annotation(
        NewAnnotation("A", "highlight", 0, (1, 2, 3, 4), "原文", "我的笔记", ((1, 2, 2, 2),)), "t1"
    )
    deleted = store.reader.add_annotation(NewAnnotation("A", "note", 3, None, None, "删除"), "t0")
    store.reader.delete_annotation(deleted.id, "t2")
    other = store.reader.add_annotation(NewAnnotation("B", "note", 9, None, None, "另篇"), "t2")
    store.papers.save_fulltext(FullTextState("A", "parsed", "a" * 64, "doc-key"), "t0")
    exported = {item.paper.id: item for item in store.export_snapshot().papers}
    assert exported["A"].aliases == (Alias("a", "first"), Alias("a", "second"), Alias("z", "last"))
    assert exported["B"].aliases == (Alias("b", "only-B"),)
    assert exported["A"].reading == store.reader.get_state("A")
    assert exported["A"].annotations == (note,) and exported["B"].annotations == (other,)
    assert exported["A"].pdf_sha256 == "a" * 64
    assert exported["B"].pdf_sha256 is None and exported["C"].annotations == ()


def test_all_extraction_versions_preserve_source_columns_and_decoded_data(store):
    for id_ in ("B", "A"):
        store.papers.insert(paper(id_))
    for id_, hash_, version, now in (
        ("A", "h1", "v1", "t9"),
        ("B", "hb", "vb", "t0"),
        ("A", "h2", "v2", "t1"),
    ):
        store.papers.insert_extraction(
            id_, hash_, version, {"summary": "模型摘要", "quote": "原话"}, now
        )
    first, second = store.export_snapshot().papers
    assert tuple(item["input_hash"] for item in first.extractions) == ("h1", "h2")
    assert tuple(item["extractor_version"] for item in first.extractions) == ("v1", "v2")
    assert tuple(item["created_at"] for item in first.extractions) == ("t9", "t1")
    assert first.extractions[0] == {
        "id": 1,
        "paper_id": "A",
        "input_hash": "h1",
        "extractor_version": "v1",
        "data": {"summary": "模型摘要", "quote": "原话"},
        "created_at": "t9",
    }
    assert len(second.extractions) == 1 and second.extractions[0]["paper_id"] == "B"


def test_all_assessments_and_policy_scores_preserve_versions_model_time_and_json(store):
    for id_ in ("A", "B"):
        store.papers.insert(paper(id_))
    for index, (id_, topic, hash_) in enumerate(
        (("A", "topic", "old"), ("B", "topic", "new"), ("A", "other", "new"))
    ):
        store.judgments.insert_assessment(
            NewAssessment(
                id_,
                topic,
                hash_,
                f"semantic-{index}",
                f"input-{index}",
                {"novelty": index},
                {"novelty": {"quote": "作者证据", "page": 0}},
                True,
                f"model-{index}",
                f"prompt-{index}",
                (GoalLink(goal_id="G", reuse="模型理由", quote="作者引文", page=0, verified=True),),
            ),
            f"t{index}",
        )
    for policy in ("z-policy", "a-policy"):
        store.judgments.save_score(
            1, policy, ScoreResult(85, "must_read", False, False, ("unclear",)), "score-time"
        )
    exported = {item.paper.id: item for item in store.export_snapshot().papers}
    versions = exported["A"].assessments
    assert tuple(item["id"] for item in versions) == (1, 3)
    assert tuple(item["model"] for item in versions) == ("model-0", "model-2")
    assert tuple(item["prompt_version"] for item in versions) == ("prompt-0", "prompt-2")
    assert tuple(item["created_at"] for item in versions) == ("t0", "t2")
    assert versions[0]["levels"] == {"novelty": 0}
    assert versions[0]["evidence"] == {"novelty": {"quote": "作者证据", "page": 0}}
    assert versions[0]["goal_links"][0]["verified"] is True
    assert versions[0]["fulltext_used"] is True
    scores = versions[0]["scores"]
    assert tuple(item["policy_hash"] for item in scores) == ("a-policy", "z-policy")
    assert scores[0] == {
        "assessment_id": 1,
        "policy_hash": "a-policy",
        "score": 85,
        "tier": "must_read",
        "provisional": False,
        "undetermined": ["unclear"],
        "created_at": "score-time",
    }
    assert versions[1]["scores"] == [] and exported["B"].assessments[0]["id"] == 2
    assert set(versions[0]) == set(assessment.c.keys()) | {"scores"}


def test_questions_and_answers_export_all_history_beyond_fifty_with_stable_time_id_order(store):
    for id_ in ("B", "A"):
        store.papers.insert(paper(id_))
    questions = tuple(question("A", n, "t0" if n == 60 else "t1") for n in range(61))
    for item in reversed(questions):
        store.qa.add_question(item)
        store.qa.add_answer(answer(item, item.created_at))
    foreign = question("B", 0)
    store.qa.add_question(foreign)
    store.qa.add_answer(answer(foreign))
    exported = {item.paper.id: item for item in store.export_snapshot().papers}
    assert len(store.qa.questions("A")) == 50
    assert exported["A"].questions == tuple(sorted(questions, key=lambda q: (q.created_at, q.id)))
    replies = tuple(answer(item, item.created_at) for item in questions)
    assert exported["A"].answers == tuple(sorted(replies, key=lambda a: (a.created_at, a.id)))
    assert exported["B"].questions == (foreign,) and exported["B"].answers == (answer(foreign),)
    assert exported["A"].questions[0].page == 0
    assert exported["A"].answers[0].statements[1].citations[0].page == 4


def test_concept_aliases_and_occurrences_keep_languages_zero_pages_and_original_locations(store):
    for id_ in ("B", "A"):
        store.papers.insert(paper(id_))
    original = store.knowledge.register("Spectral Method", "en", "t0")
    unused = store.knowledge.register("Unused", None, "t0")
    store.knowledge.add_alias(original.id, "谱方法", "zh-CN", "t1")
    for id_, page, now in (("B", 9, "t0"), ("A", 0, "t0")):
        store.knowledge.add_occurrence(
            ConceptOccurrence(
                original.id,
                id_,
                "core",
                "An original verified definition.",
                page,
                (1, 2, 3, 4),
                True,
                f"hash-{id_}",
                "producer-v1",
                now,
            )
        )
    exported = store.export_snapshot().concepts
    assert tuple(item.concept.id for item in exported) == tuple(sorted((original.id, unused.id)))
    loaded = next(item for item in exported if item.concept.id == original.id)
    assert loaded.concept == original and loaded.aliases == store.knowledge.aliases(original.id)
    assert loaded.occurrences == store.knowledge.occurrences(original.id)
    assert tuple(item.paper_id for item in loaded.occurrences) == ("A", "B")
    assert loaded.occurrences[0].page == 0 and loaded.occurrences[0].bbox == (1, 2, 3, 4)
    assert next(item for item in exported if item.concept.id == unused.id).occurrences == ()
    assert store.export_snapshot().concepts == exported


@pytest.mark.parametrize("count", [0, 10, 1000])
def test_snapshot_uses_twelve_parameterless_selects_on_one_connection_and_never_writes(
    store, count, monkeypatch
):
    with store.atomic():
        for n in reversed(range(count)):
            store.papers.insert(paper(f"P{n:04}"))
    with store._db._engine.connect() as connection:
        connection.connection.driver_connection.setlimit(sqlite3.SQLITE_LIMIT_VARIABLE_NUMBER, 999)
    statements, connections = [], set()

    def capture(conn, _cursor, statement, parameters, _context, _many):
        statements.append((statement, parameters))
        connections.add(id(conn))

    def reject_write(_statement):
        pytest.fail("snapshot must not write")

    monkeypatch.setattr(store._db, "write", reject_write)
    event.listen(store._db._engine, "before_cursor_execute", capture)
    try:
        snapshot = store.export_snapshot()
    finally:
        event.remove(store._db._engine, "before_cursor_execute", capture)
    selects = [(sql, params) for sql, params in statements if sql.startswith("SELECT")]
    assert len(selects) == 12 and len(connections) == 1
    assert all(not params for _sql, params in selects)
    assert all(sql.startswith(("SELECT", "BEGIN")) for sql, _params in statements)
    assert tuple(item.paper.id for item in snapshot.papers) == tuple(
        f"P{n:04}" for n in range(count)
    )
    assert store._db._active is None


def test_snapshot_is_consistent_when_another_writer_changes_metadata_and_reading(store):
    store.papers.insert(paper("A"))
    writer = SqlStore(str(store._db._engine.url))
    started = Event()
    futures = []

    def change():
        started.set()
        with writer.atomic():
            writer.papers.fill_missing("A", {"abstract": "新摘要"})
            writer.reader.set_state("A", "read", {"page": 4}, "t2")

    with ThreadPoolExecutor(max_workers=1) as executor:

        def capture(_conn, _cursor, statement, _params, _context, _many):
            if statement.startswith("SELECT") and not futures:
                futures.append(executor.submit(change))
                assert started.wait(5)

        event.listen(store._db._engine, "after_cursor_execute", capture)
        try:
            exported = store.export_snapshot().papers[0]
        finally:
            event.remove(store._db._engine, "after_cursor_execute", capture)
        assert exported.paper.abstract is None and exported.reading is None
        futures[0].result(timeout=5)
    writer.dispose()
    updated = store.export_snapshot().papers[0]
    assert updated.paper.abstract == "新摘要" and updated.reading.status == "read"
