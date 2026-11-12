"""重要前作使用完整文库计票，保留引文来源并遵守只读接口边界。"""

from dataclasses import replace

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import event

from litgleaner.application import memory
from litgleaner.domain.errors import InvalidRequest
from litgleaner.domain.identity import Alias
from litgleaner.domain.inbox import InboxItem
from litgleaner.domain.reading import NewAnnotation
from litgleaner.server.app import create_app
from litgleaner.server.security import LocalSecurity
from tests.e2e.memory_helpers import add, citation

BASE = "/api/memory/important-works"
TARGET = (Alias("doi", "10.1000/prior"),)


def test_empty_library_has_zero_threshold_and_no_important_works(harness):
    add(harness, "candidate", library=False)
    result = memory.important_works(harness.ctx)
    assert result.library_size == result.threshold == result.total == 0
    assert result.works == () and result.limit == 50 and result.offset == 0


def test_uncited_library_papers_change_threshold_without_rewriting_evidence(harness):
    a, b = add(harness, "a"), add(harness, "b")
    add(harness, "c")
    original = citation(harness, a, aliases=TARGET)
    first = memory.important_works(harness.ctx)
    assert (first.library_size, first.threshold, first.total) == (3, 1, 1)
    add(harness, "uncited")
    second = memory.important_works(harness.ctx)
    assert (second.library_size, second.threshold, second.total) == (4, 2, 0)
    citation(harness, b, aliases=TARGET)
    (work,) = memory.important_works(harness.ctx).works
    assert work.citing_count == 2
    assert next(s for s in work.citing_papers if s.paper.paper_id == a).citations == (original,)


def test_multiple_sources_and_positions_preserve_original_evidence_but_only_one_vote(harness):
    a, b = add(harness, "a"), add(harness, "b")
    for name in ("c", "d"):
        add(harness, name)
    one = citation(harness, a, aliases=TARGET)
    two = citation(harness, a, aliases=TARGET, position="ref:1")
    remote = citation(harness, a, aliases=TARGET, source="another", position="remote:0")
    assert memory.important_works(harness.ctx).total == 0
    other = citation(harness, b, aliases=TARGET)
    (work,) = memory.important_works(harness.ctx).works
    assert work.citing_count == 2 and work.target.paper_id is None
    supports = {s.paper.paper_id: s for s in work.citing_papers}
    assert supports[a].citations == (remote, one, two)
    assert supports[b].citations == (other,)
    assert supports[a].paper.reading_status == "queued"
    assert supports[a].citations[0].contexts == ("We use the referenced method.",)
    assert supports[a].citations[0].page == 3
    assert supports[a].citations[0].bbox == (10, 20, 30, 40)


def test_candidate_citations_never_vote_until_paper_enters_library(harness):
    a = add(harness, "a")
    for name in ("b", "c", "d"):
        add(harness, name)
    candidate = add(harness, "candidate", library=False)
    citation(harness, a, aliases=TARGET)
    citation(harness, candidate, aliases=TARGET)
    assert memory.important_works(harness.ctx).total == 0
    harness.ctx.store.reader.set_state(candidate, "reading", None, harness.ctx.now_iso())
    result = memory.important_works(harness.ctx)
    assert (result.library_size, result.threshold, result.works[0].citing_count) == (5, 2, 2)


def test_self_link_is_not_a_vote_and_other_papers_still_count(harness):
    target, a, b = (add(harness, name) for name in ("prior", "a", "b"))
    add(harness, "c")
    citation(harness, target, target=target)
    first = citation(harness, a, target=target)
    assert memory.important_works(harness.ctx).total == 0
    second = citation(harness, b, aliases=TARGET)
    (work,) = memory.important_works(harness.ctx).works
    assert work.target.paper_id == target and work.citing_count == 2
    assert {s.paper.paper_id for s in work.citing_papers} == {a, b}
    assert {c for s in work.citing_papers for c in s.citations} == {first, second}


def test_target_arriving_outside_library_resolves_identity_without_changing_votes(harness):
    a, b = add(harness, "a"), add(harness, "b")
    citation(harness, a, aliases=TARGET)
    citation(harness, b, aliases=TARGET)
    before = memory.important_works(harness.ctx)
    assert before.works[0].target.paper_id is None
    target = add(harness, "prior", library=False)
    after = memory.important_works(harness.ctx)
    assert after.library_size == before.library_size == 2
    assert after.works[0].target.paper_id == target
    assert after.works[0].citing_papers == before.works[0].citing_papers


def test_ambiguous_global_aliases_and_unidentified_titles_never_gain_votes(harness):
    a, b = add(harness, "a"), add(harness, "b")
    add(harness, "prior", library=False)
    add(harness, "outside", library=False)
    ambiguous = (*TARGET, Alias("doi", "10.1000/outside"))
    citation(harness, a, aliases=ambiguous)
    citation(harness, b, aliases=ambiguous)
    citation(harness, a, position="title-only")
    citation(harness, b, position="title-only")
    assert memory.important_works(harness.ctx).works == ()


def test_note_inbox_all_reading_states_and_soft_delete_share_actual_library_scope(harness):
    ctx = harness.ctx
    ids = {name: add(harness, name, library=False) for name in ("note", "inbox", "deleted")}
    note = ctx.store.reader.add_annotation(
        NewAnnotation(ids["note"], "note", None, None, None, "Personal note"), ctx.now_iso()
    )
    deleted = ctx.store.reader.add_annotation(
        NewAnnotation(ids["deleted"], "note", None, None, None, "Removed note"), ctx.now_iso()
    )
    ctx.store.reader.delete_annotation(deleted.id, ctx.now_iso())
    ctx.store.inbox.add(InboxItem("in", "doi", "10.1000/inbox", "pending", ctx.now_iso()))
    ctx.store.inbox.finish("in", "done", ids["inbox"], None, ctx.now_iso())
    for status in ("queued", "reading", "read", "abandoned"):
        paper_id = add(harness, status)
        ctx.store.reader.set_state(paper_id, status, None, ctx.now_iso())
    citation(harness, ids["note"], aliases=TARGET)
    citation(harness, ids["inbox"], aliases=TARGET)
    result = memory.important_works(ctx)
    assert (result.library_size, result.threshold, result.total) == (6, 2, 1)
    assert {s.paper.reading_status for s in result.works[0].citing_papers} == {"new"}
    ctx.store.reader.delete_annotation(note.id, ctx.now_iso())
    result = memory.important_works(ctx)
    assert (result.library_size, result.threshold, result.total) == (5, 2, 0)


def test_api_exposes_complete_paging_original_evidence_and_local_security(harness):
    a = add(harness, "a")
    with harness.ctx.store.atomic():
        for n in reversed(range(56)):
            citation(harness, a, aliases=(Alias("doi", f"10.1000/target-{n:02}"),), position=str(n))
    web = create_app(harness.ctx, LocalSecurity(48143, launch_token="important-launch"), None)
    with TestClient(web, base_url="http://127.0.0.1:48143") as client:
        assert client.get(BASE).status_code == 401
        client.get("/launch", params={"token": "important-launch"})
        default = client.get(BASE).json()
        assert default["library_size"] == default["threshold"] == 1
        assert default["total"] == 56 and len(default["works"]) == 50
        pages = [client.get(BASE, params={"limit": 20, "offset": n}).json() for n in (0, 20, 40)]
        works = [work for page in pages for work in page["works"]]
        assert all(page["total"] == 56 for page in pages)
        assert [w["target"]["aliases"][0]["value"] for w in works] == [
            f"10.1000/target-{n:02}" for n in range(56)
        ]
        support = works[0]["citing_papers"][0]
        assert support["paper"]["paper_id"] == a and works[0]["citing_count"] == 1
        assert support["citations"][0]["raw_text"] == "Original reference entry"
        assert support["citations"][0]["contexts"] == ["We use the referenced method."]
        assert client.get(BASE, params={"offset": 100}).json()["works"] == []
        for params in ({"limit": 0}, {"limit": 201}, {"offset": -1}, {"limit": "bad"}):
            assert client.get(BASE, params=params).status_code == 422
        assert client.get(BASE, headers={"Origin": "https://other.test"}).status_code == 403
        assert client.get(BASE, headers={"Host": "other.test"}).status_code == 400
        assert client.post(BASE).status_code == 403  # 写方法依旧需要 CSRF。


@pytest.mark.parametrize("args", [{"limit": 0}, {"limit": 201}, {"offset": -1}])
def test_invalid_application_paging_rejects_before_reading_snapshot(harness, monkeypatch, args):
    def forbidden():
        pytest.fail("invalid paging should not read the database")

    monkeypatch.setattr(type(harness.ctx.store), "memory_snapshot", forbidden)
    with pytest.raises(InvalidRequest):
        memory.important_works(harness.ctx, **args)


@pytest.mark.parametrize("count", [0, 75])
def test_query_is_eight_selects_on_one_connection_without_io_or_model(harness, monkeypatch, count):
    with harness.ctx.store.atomic():
        for n in range(count):
            citation(harness, add(harness, f"paper-{n}"), aliases=TARGET)
    ctx = replace(harness.ctx, llm=replace(harness.ctx.llm, routes={}))
    db = ctx.store._db
    statements, connections = [], set()

    def captured(conn, _cursor, sql, _params, _context, _many):
        statements.append(sql)
        connections.add(id(conn))
        assert sql.startswith(("SELECT", "BEGIN"))

    def forbidden(*_args, **_kwargs):
        pytest.fail("important works must not write, read blobs, or call external services")

    def outside_transaction(papers, index):
        assert db._active is None
        return project(papers, index)

    project = memory.project_important_works
    monkeypatch.setattr(memory, "project_important_works", outside_transaction)
    monkeypatch.setattr(db, "write", forbidden)
    monkeypatch.setattr(type(ctx.llm), "call", forbidden)
    monkeypatch.setattr(type(ctx.blobs), "get", forbidden)
    monkeypatch.setattr(type(ctx.blobs), "put", forbidden)
    for source in ctx.sources.values():
        monkeypatch.setattr(type(source), "fetch", forbidden)
    event.listen(db._engine, "before_cursor_execute", captured)
    try:
        result = memory.important_works(ctx)
    finally:
        event.remove(db._engine, "before_cursor_execute", captured)
    assert len([s for s in statements if s.startswith("SELECT")]) == 8
    assert len(connections) == 1 and result.library_size == count
    assert result.total == bool(count)
    if count:
        assert result.works[0].citing_count == count


def test_library_changes_after_snapshot_do_not_mix_denominator_and_votes(harness, monkeypatch):
    a = add(harness, "a")
    add(harness, "b")
    add(harness, "c")
    citation(harness, a, aliases=TARGET)
    project = memory.project_important_works

    def add_after_snapshot(papers, index):
        assert harness.ctx.store._db._active is None
        add(harness, "concurrent")
        return project(papers, index)

    with monkeypatch.context() as patch:
        patch.setattr(memory, "project_important_works", add_after_snapshot)
        result = memory.important_works(harness.ctx)
    assert (result.library_size, result.threshold, result.total) == (3, 1, 1)
    result = memory.important_works(harness.ctx)
    assert (result.library_size, result.threshold, result.total) == (4, 2, 0)
