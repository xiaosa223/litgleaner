"""推送后用户选“加入文献库”：记录反馈，library 阶段写入，重试和去重都由任务队列负责。"""

import pytest

from litgleaner.application.library import find_paper, record_action, save_paper
from litgleaner.domain.errors import ConfigError, Deferred
from litgleaner.domain.library import CardAction
from tests.e2e.conftest import Harness, paper


@pytest.fixture
def pushed(harness: Harness) -> Harness:
    harness.edit_yaml("app.yaml", lambda d: d.update(library={"adapter": "fake_library"}))
    harness.rebuild()
    harness.write_papers([paper("Top Paper", 5), paper("NoPDF Paper", 4)])
    report = harness.run()
    assert report.ok, report.summary()
    return harness


def paper_id(h: Harness, title: str) -> str:
    return find_paper(h.ctx, f"10.1000/{title.lower().replace(' ', '-')}")


def test_click_then_run_saves_once_with_note_tags_and_pdf(pushed: Harness):
    pid = paper_id(pushed, "Top Paper")
    action = CardAction("save_to_library", pid, None)
    assert record_action(pushed.ctx, action, channel="feishu", actor="ou_me", external_id="ev1")
    # 同一次点击重复送达只记一次。
    assert not record_action(pushed.ctx, action, channel="feishu", external_id="ev1")

    report = pushed.run(only="library")
    assert report.ok, report.summary()
    (saved,) = pushed.ctx.library.saved  # type: ignore[union-attr]
    assert saved.title == "Top Paper"
    assert saved.doi == "10.1000/top-paper"
    assert saved.pdf and saved.pdf.startswith(b"%PDF")
    assert "Alpha" in saved.tags
    assert "阅读价值" in saved.note_html and pid in saved.note_html

    assert pushed.run(only="library").stages[0].done == 0
    entry = pushed.ctx.store.feedback.get_entry(pid, "fake_library")
    assert entry is not None and entry.created


def test_paper_without_pdf_is_saved_without_attachment(pushed: Harness):
    pid = paper_id(pushed, "NoPDF Paper")
    record_action(pushed.ctx, CardAction("save_to_library", pid), channel="cli")
    pushed.run(only="library")
    (saved,) = pushed.ctx.library.saved  # type: ignore[union-attr]
    assert saved.pdf is None


def test_dismiss_after_save_cancels_pending_save(pushed: Harness):
    pid = paper_id(pushed, "Top Paper")
    record_action(pushed.ctx, CardAction("save_to_library", pid), channel="cli")
    record_action(pushed.ctx, CardAction("dismiss", pid), channel="cli")
    pushed.run(only="library")
    assert pushed.ctx.library.saved == []  # type: ignore[union-attr]


def test_library_not_running_is_postponed_then_saved(pushed: Harness):
    pid = paper_id(pushed, "Top Paper")
    record_action(pushed.ctx, CardAction("save_to_library", pid), channel="cli")
    pushed.ctx.library.fail_next.append(Deferred("连不上 Zotero", 900))  # type: ignore[union-attr]
    report = pushed.run(only="library")
    assert report.stages[0].retried == 1
    assert pushed.ctx.store.feedback.get_entry(pid, "fake_library") is None
    pushed.clock.advance(minutes=16)
    assert pushed.run(only="library").ok
    assert pushed.ctx.store.feedback.get_entry(pid, "fake_library") is not None


def test_unknown_paper_is_ignored_and_reference_lookup_reports_missing(pushed: Harness):
    assert not record_action(pushed.ctx, CardAction("save_to_library", "NOPE"), channel="feishu")
    with pytest.raises(ConfigError, match="库里没有这篇论文"):
        find_paper(pushed.ctx, "10.9999/missing")


def test_saving_without_library_configured_is_a_clear_error(harness: Harness):
    with pytest.raises(ConfigError, match="没有启用文献库"):
        save_paper(harness.ctx, "anything")
