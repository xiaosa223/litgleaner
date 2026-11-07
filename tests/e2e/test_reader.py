"""领读器的查询和操作：首页、阅读页、阅读进度、批注。HTTP 层只调用这些函数。"""

import pytest

from litgleaner.application import reader
from litgleaner.domain.errors import NotFound
from litgleaner.domain.reading import NewAnnotation
from tests.e2e.conftest import Harness, paper


@pytest.fixture
def pushed(harness: Harness) -> Harness:
    harness.write_papers([paper("Top Paper", 5), paper("NoPDF Paper", 4)])
    harness.run()
    assert harness.pushed_titles() == ["Top Paper", "NoPDF Paper"]
    return harness


def _paper_id(harness: Harness, title: str) -> str:
    items = reader.inbox(harness.ctx).recommended
    return next(item.paper_id for item in items if item.title == title)


def test_inbox_shows_active_topic_profiles(pushed: Harness):
    (topic,) = reader.inbox(pushed.ctx).topics
    assert (topic.id, topic.name) == ("alpha-topic", "Alpha")
    assert [(goal.id, goal.text) for goal in topic.goals] == [("g1", "alpha goal")]


def test_month_cost_counts_llm_calls_since_month_start(pushed: Harness):
    assert reader.month_cost_usd(pushed.ctx) == pytest.approx(0.001 * pushed.llm_calls())


def test_inbox_lists_recommendations_with_reading_status(pushed: Harness):
    items = reader.inbox(pushed.ctx).recommended
    assert [item.title for item in items] == ["Top Paper", "NoPDF Paper"]
    top = items[0]
    assert (top.topic_id, top.topic_name, top.tier_label) == ("alpha-topic", "Alpha", "必读")
    assert top.reading_status is None
    assert reader.inbox(pushed.ctx).queued == ()

    reader.set_reading_state(pushed.ctx, top.paper_id, "queued", None)
    inbox = reader.inbox(pushed.ctx)
    assert [item.title for item in inbox.queued] == ["Top Paper"]
    assert inbox.recommended[0].reading_status == "queued"


def test_paper_view_combines_facts_judgments_and_user_data(pushed: Harness):
    paper_id = _paper_id(pushed, "Top Paper")
    view = reader.paper_view(pushed.ctx, paper_id)

    assert view.title == "Top Paper"
    assert view.doi == "10.1000/top-paper"
    assert view.identifiers["doi"] == "10.1000/top-paper"
    assert view.fulltext_status == "parsed"
    assert view.has_pdf and view.has_document
    assert view.extraction is not None and view.extraction["summary"] == "演示总结"
    assert view.assessments["alpha-topic"]["tier"] == "must_read"
    assert view.assessments["alpha-topic"]["topic_name"] == "Alpha"
    assert view.assessments["alpha-topic"]["tier_label"] == "必读"
    assert view.reading is None
    assert view.annotations == ()


def test_abstract_only_paper_has_no_document_or_pdf(pushed: Harness):
    paper_id = _paper_id(pushed, "NoPDF Paper")
    view = reader.paper_view(pushed.ctx, paper_id)
    assert (view.has_pdf, view.has_document) == (False, False)
    assert reader.document_view(pushed.ctx, paper_id) is None
    with pytest.raises(NotFound):
        reader.pdf_content(pushed.ctx, paper_id)


def test_document_view_keeps_page_and_block_order(pushed: Harness):
    paper_id = _paper_id(pushed, "Top Paper")
    document = reader.document_view(pushed.ctx, paper_id)
    assert document is not None
    assert document.quality == "ok"
    assert [block.index for block in document.blocks] == list(range(len(document.blocks)))
    assert document.blocks[1].kind == "heading"
    assert [b.translatable for b in document.blocks] == [True, False, True, False, True]
    assert reader.pdf_content(pushed.ctx, paper_id).startswith(b"%PDF")


def test_reading_state_records_position(pushed: Harness):
    paper_id = _paper_id(pushed, "Top Paper")
    reader.set_reading_state(pushed.ctx, paper_id, "reading", {"view": "pdf", "page": 2})
    view = reader.paper_view(pushed.ctx, paper_id)
    assert view.reading is not None
    assert (view.reading.status, view.reading.position) == ("reading", {"view": "pdf", "page": 2})


def test_annotations_add_edit_remove(pushed: Harness):
    paper_id = _paper_id(pushed, "Top Paper")
    note = reader.add_annotation(
        pushed.ctx, NewAnnotation(paper_id, "note", 1, None, "We found it works.", "要复现")
    )
    reader.edit_annotation(pushed.ctx, note.id, "要先复现基线")
    assert [a.body for a in reader.paper_view(pushed.ctx, paper_id).annotations] == ["要先复现基线"]
    reader.remove_annotation(pushed.ctx, note.id)
    assert reader.paper_view(pushed.ctx, paper_id).annotations == ()
    with pytest.raises(NotFound):
        reader.edit_annotation(pushed.ctx, note.id, "x")


def test_unknown_paper_is_not_found(pushed: Harness):
    for call in (
        lambda: reader.paper_view(pushed.ctx, "missing"),
        lambda: reader.set_reading_state(pushed.ctx, "missing", "read", None),
        lambda: reader.add_annotation(
            pushed.ctx, NewAnnotation("missing", "note", None, None, None, "x")
        ),
    ):
        with pytest.raises(NotFound):
            call()


def test_queued_paper_that_was_never_pushed_still_shows(harness: Harness):
    harness.write_papers([paper("Weak Paper", 1)])
    harness.run()
    (paper_id,) = harness.ctx.store.judgments.papers_with_scores()
    assert reader.inbox(harness.ctx).recommended == ()

    reader.set_reading_state(harness.ctx, paper_id, "queued", None)

    (item,) = reader.inbox(harness.ctx).queued
    assert (item.title, item.score, item.topic_id) == ("Weak Paper", None, None)
    assert item.reading_status == "queued"


def test_paper_being_read_stays_on_home_page(harness: Harness):
    harness.write_papers([paper("Weak Paper", 1)])
    harness.run()
    (paper_id,) = harness.ctx.store.judgments.papers_with_scores()

    reader.set_reading_state(harness.ctx, paper_id, "reading", {"page": 2})

    inbox = reader.inbox(harness.ctx)
    assert inbox.queued == ()
    (item,) = inbox.reading
    assert (item.title, item.reading_status) == ("Weak Paper", "reading")
