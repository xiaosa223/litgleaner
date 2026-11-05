"""独立代码审查发现的问题。每条都先复现，再修复。"""

import json
from collections.abc import Mapping
from typing import Any

import pytest

from litfactory.application.delivery_ops import resolve_delivery, unresolved_deliveries
from litfactory.application.pipeline import run_stage
from litfactory.application.rubric_ops import rescore
from litfactory.application.stages.base import Stage
from litfactory.application.topics import activate_topic, pause_topic
from litfactory.domain.errors import ConfigError, PermanentError, SchemaChanged
from litfactory.ports.store import JobSpec
from tests.e2e.conftest import Harness, paper


def archived(harness: Harness) -> dict:
    return json.loads(harness.archive_files()[0].read_text(encoding="utf-8"))


def test_retrying_dead_extract_upgrades_paper_to_fulltext(harness: Harness):
    harness.write_papers([paper("Recover", 5)])
    harness.llm.fail_tasks["extract"] = [PermanentError("模拟提取失败")]
    harness.run()
    assert archived(harness)["assessments"]["alpha-topic"]["fulltext_used"] is False

    harness.ctx.store.jobs.retry_dead(harness.ctx.now_iso())
    harness.clock.advance(minutes=1)
    harness.run()

    assert harness.ctx.store.papers.get_fulltext(archived(harness)["id"]).status == "parsed"
    assert archived(harness)["assessments"]["alpha-topic"]["fulltext_used"] is True


def test_archive_comes_back_after_rubric_rollback(harness: Harness):
    harness.write_papers([paper("Mid", 3)])
    harness.run()
    assert harness.archive_files()

    original = {}

    def raise_bar(data: dict) -> None:
        original.update(json.loads(json.dumps(data)))
        for tier in data["tiers"]:
            if tier["key"] != "drop":
                tier["min_score"] = 95

    harness.edit_rubric(raise_bar)
    harness.rebuild()
    rescore(harness.ctx)
    assert harness.archive_files() == []

    harness.edit_rubric(lambda data: data.update(original))
    harness.rebuild()
    rescore(harness.ctx)
    assert harness.archive_files(), "恢复指标后归档必须回来"


def test_overlapping_routes_do_not_publish_empty_digest(harness: Harness):
    def add_route(data: dict) -> None:
        data["routes"] = [
            data["routes"][0],
            {**data["routes"][0], "id": "second-me"},
        ]

    harness.edit_yaml("channels.yaml", add_route)
    harness.rebuild()
    harness.write_papers([paper("Only One", 5)])
    harness.run()
    assert len(harness.publisher().sent) == 1


class _BrokenOnDead(Stage):
    name = "broken"

    def plan(self, ctx):
        yield JobSpec("broken:1", self.name, {})
        yield JobSpec("broken:2", self.name, {})

    def process(self, ctx, payload: Mapping[str, Any]) -> None:
        raise PermanentError("process failed")

    def on_dead(self, ctx, payload: Mapping[str, Any]) -> None:
        raise RuntimeError("cleanup failed")


def test_failing_on_dead_hook_does_not_abort_stage(harness: Harness):
    report = run_stage(harness.ctx, _BrokenOnDead())
    assert report.dead == 2
    assert any("cleanup failed" in error for error in report.errors)


def test_dead_jobs_are_reported_on_every_run(harness: Harness):
    harness.write_papers([paper("Any", 5)])
    harness.publisher().crash_after_send = True
    harness.run()
    harness.clock.advance(hours=1)
    harness.run()
    harness.clock.advance(hours=1)
    assert any("死信" in warning for warning in harness.run().warnings)


def _crash_send(harness: Harness) -> tuple[str, str]:
    harness.write_papers([paper("Unknown", 5)])
    harness.publisher().crash_after_send = True
    harness.run()
    harness.publisher().crash_after_send = False
    harness.clock.advance(hours=1)
    harness.run()
    return unresolved_deliveries(harness.ctx)[0]


def test_unknown_delivery_confirmed_sent_is_not_resent(harness: Harness):
    digest_id, channel = _crash_send(harness)
    assert harness.ctx.store.outputs.delivery_status(digest_id, channel) == "sending"
    resolve_delivery(harness.ctx, digest_id, channel, resend=False)
    assert unresolved_deliveries(harness.ctx) == ()
    harness.clock.advance(hours=1)
    assert harness.run().ok
    assert len(harness.publisher().sent) == 1


def test_unknown_delivery_can_be_resent_after_human_decision(harness: Harness):
    digest_id, channel = _crash_send(harness)
    resolve_delivery(harness.ctx, digest_id, channel, resend=True)
    harness.clock.advance(hours=1)
    harness.run()
    assert len(harness.publisher().sent) == 2
    assert unresolved_deliveries(harness.ctx) == ()


def test_resolve_rejects_unknown_delivery(harness: Harness):
    with pytest.raises(ConfigError, match="不存在"):
        resolve_delivery(harness.ctx, "nope", "me", resend=False)


def test_delivery_claim_is_compare_and_set(harness: Harness):
    harness.write_papers([paper("Claim", 5)])
    harness.publisher().fail_next.append(SchemaChanged("x"))
    harness.run()
    digest_id, channel = unresolved_deliveries(harness.ctx)[0]
    outputs = harness.ctx.store.outputs
    assert outputs.claim_delivery(digest_id, channel, "t") is True
    assert outputs.claim_delivery(digest_id, channel, "t") is False


def test_topic_changed_while_paused_does_not_bill_history(harness: Harness):
    harness.write_papers([paper("History", 5)])
    harness.run()
    pause_topic(harness.ctx, "alpha-topic")
    harness.edit_yaml("topics/alpha-topic.yaml", lambda d: d.update(description="new focus"))
    harness.rebuild()
    harness.clock.advance(minutes=1)
    activate_topic(harness.ctx, "alpha-topic")
    harness.run()
    assert harness.llm_calls("triage") == 1


def test_llm_output_length_is_bounded(harness: Harness):
    harness.write_papers([paper("Verbose", 5)])
    harness.llm.overrides["extract"] = {
        "summary": "很长" * 1000,
        "conclusion": None,
        "innovation": None,
        "limitations": [],
    }
    assert not harness.run().ok
    assert harness.ctx.store.stats()["extraction"] == 0


def test_links_from_llm_never_reach_any_channel(harness: Harness):
    harness.write_papers([paper("Injected", 5)])
    harness.llm.overrides["extract"] = {
        "summary": "访问 https://evil.example/x 领奖",
        "conclusion": "详见 www.evil.example/y 的说明",
        "innovation": "[点击](http://evil.example/z)",
        "limitations": [{"text": "见 https://evil.example", "source": "inferred", "quote": None}],
    }
    harness.run()
    body = harness.publisher().sent[0].body
    assert "evil.example" not in body
    assert "https://doi.org/" in body
