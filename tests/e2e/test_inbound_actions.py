"""真实反馈和文献库写入前，按卡片的原渠道及订阅人二次核验。"""

from dataclasses import replace

import pytest
from sqlalchemy import select

from litgleaner.application.inbound import apply_click, click_channel
from litgleaner.domain.errors import Deferred
from litgleaner.domain.library import CardAction, decode_action
from litgleaner.infra.db.schema import feedback
from litgleaner.ports.inbound import CardClick
from litgleaner.ports.store import DigestItem, DigestRecord
from tests.e2e.conftest import Harness
from tests.e2e.test_feishu_click import save_button_values, setup


@pytest.fixture
def action_case(harness: Harness):
    values = save_button_values(setup(harness))
    action = decode_action(values[0])
    assert action is not None
    return harness, CardClick(action, "ou_me", "event-1", "message-1")


def stored(harness):
    return harness.ctx.store._db.fetch(select(feedback))


def test_click_only_runs_library_and_replay_never_repeats_feedback_or_save(action_case):
    harness, click = action_case
    before = harness.llm_calls()
    allowed = {"me": frozenset({"ou_me"})}
    rendered = apply_click(harness.ctx, click, allowed)
    assert rendered is not None and "已加入 Zotero" in rendered.body
    assert len(stored(harness)) == len(harness.ctx.library.saved) == 1
    assert apply_click(harness.ctx, click, allowed) == rendered
    assert len(stored(harness)) == len(harness.ctx.library.saved) == 1
    assert harness.llm_calls() == before


@pytest.mark.parametrize("bad", ["actor", "digest", "membership", "disabled", "channel"])
def test_unverifiable_clicks_never_write_feedback_or_library(action_case, bad):
    harness, click = action_case
    allowed = {"me": frozenset({"ou_me"}), "other": frozenset({"ou_other"})}
    ctx = harness.ctx
    if bad == "actor":
        click = replace(click, actor="ou_other")
    elif bad == "digest":
        click = replace(click, action=replace(click.action, digest_id="missing"))
    elif bad == "membership":
        click = replace(click, action=replace(click.action, paper_id="not-in-this-card"))
    elif bad == "disabled":
        allowed.pop("me")
    else:
        ctx = replace(ctx, channels={})
    before = harness.llm_calls()
    assert click_channel(ctx, click, allowed) is None
    assert apply_click(ctx, click, allowed) is None
    assert stored(harness) == [] and harness.ctx.library.saved == []
    assert harness.llm_calls() == before


def test_two_channels_use_digest_channel_instead_of_whitelist_order(action_case):
    harness, click = action_case
    ctx = harness.ctx
    original = ctx.store.outputs.get_digest(click.action.digest_id)
    card = ctx.store.outputs.digest_cards(original.id)[0]
    ctx.store.outputs.create_digest(
        DigestRecord("other-digest", "other-route", original.topic_id, "other", "week"),
        (DigestItem(card["paper_id"], False, card),),
        ctx.now_iso(),
    )
    ctx = replace(
        ctx, channels={**ctx.channels, "other": replace(ctx.channels["me"], name="other")}
    )
    allowed = {"me": frozenset({"ou_me"}), "other": frozenset({"ou_other"})}
    click = replace(click, actor="ou_other", action=replace(click.action, digest_id="other-digest"))
    assert click_channel(ctx, click, allowed) == "other"
    apply_click(ctx, click, allowed)
    assert stored(harness)[0].channel == "other"


@pytest.mark.parametrize("multi", [False, True])
def test_legacy_click_without_digest_only_accepts_one_unambiguous_channel(action_case, multi):
    harness, click = action_case
    click = replace(click, action=replace(click.action, digest_id=None))
    allowed = {"me": frozenset({"ou_me"})}
    if multi:
        allowed["other"] = frozenset({"ou_me"})
    assert click_channel(harness.ctx, click, allowed) == (None if multi else "me")
    apply_click(harness.ctx, click, allowed)
    assert len(stored(harness)) == (0 if multi else 1)


def test_unavailable_library_keeps_feedback_and_persistent_job(action_case):
    harness, click = action_case
    harness.ctx.library.fail_next.append(Deferred("library is closed"))
    before = harness.llm_calls()
    rendered = apply_click(harness.ctx, click, {"me": frozenset({"ou_me"})})
    assert rendered is not None and len(stored(harness)) == 1
    assert harness.ctx.library.saved == []
    assert harness.ctx.store.feedback.get_entry(click.action.paper_id, "fake_library") is None
    pending = [
        job for job in harness.ctx.store.jobs.list_by_status("pending") if job.stage == "library"
    ]
    assert len(pending) == 1 and pending[0].attempts == 0
    assert harness.llm_calls() == before


def test_dismiss_and_missing_library_record_feedback_without_models(action_case):
    harness, click = action_case
    before = harness.llm_calls()
    ctx = replace(harness.ctx, library=None)
    apply_click(ctx, click, {"me": frozenset({"ou_me"})})
    dismissed = replace(
        click,
        action=CardAction("dismiss", click.action.paper_id, click.action.digest_id),
        event_id="event-2",
    )
    apply_click(ctx, dismissed, {"me": frozenset({"ou_me"})})
    assert len(stored(harness)) == 2
    assert ctx.store.feedback.latest_action(click.action.paper_id) == "dismiss"
    assert harness.ctx.library.saved == [] and harness.llm_calls() == before
