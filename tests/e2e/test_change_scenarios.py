"""架构文档第 14 节的变更场景：只改配置，流程代码不动。"""

import json

import pytest
import yaml

from litgleaner.application.rubric_ops import reassess, rescore
from litgleaner.application.stages.extract import extraction_input_hash
from litgleaner.application.topics import activate_topic, backfill_topic, pause_topic
from litgleaner.bootstrap import build_context
from litgleaner.domain.errors import ConfigError, TransientError
from litgleaner.infra.config import load_config
from tests.e2e.conftest import Harness, paper


def add_source(harness: Harness, name: str, papers: list[dict]) -> None:
    harness.write_papers(papers, f"{name}.json")
    harness.edit_yaml(
        "sources.yaml",
        lambda d: d["sources"].update(
            {
                name: {
                    "adapter": "fake_source",
                    "options": {"records_file": f"config/demo/{name}.json"},
                }
            }
        ),
    )


def add_topic(harness: Harness, topic_id: str, **extra) -> None:
    data = {
        "id": topic_id,
        "name": topic_id,
        "background": f"{topic_id} background",
        "content": f"{topic_id} research",
        "goals": [{"id": "g1", "text": f"{topic_id} goal"}],
        "query": {"include": [topic_id]},
        "rubric": "default",
        **extra,
    }
    path = harness.root / "config" / "topics" / f"{topic_id}.yaml"
    path.write_text(yaml.safe_dump(data), encoding="utf-8")


def test_add_source_by_config_finds_missed_paper_and_records_overlap(harness: Harness):
    harness.write_papers([paper("Common", 5)])
    harness.run()
    harness.clock.advance(days=1)

    add_source(harness, "beta", [paper("Common", 5), paper("Only Beta", 5)])
    harness.rebuild()
    harness.run()

    assert harness.ctx.store.stats()["paper"] == 2
    assert harness.ctx.store.papers.source_contribution() == {"alpha": 1, "beta": 2}
    assert "Only Beta" in harness.pushed_titles()


def test_disable_source_stops_fetching_and_keeps_history(harness: Harness):
    harness.write_papers([paper("Old", 5)])
    harness.run()
    harness.edit_yaml("sources.yaml", lambda d: d["sources"]["alpha"].update(enabled=False))
    harness.rebuild()
    harness.clock.advance(days=1)
    harness.write_papers([paper("Old", 5), paper("Never Seen", 5)])
    harness.run()

    assert harness.ctx.store.stats()["paper"] == 1
    assert harness.ctx.store.papers.source_contribution() == {"alpha": 1}


def test_change_weights_rescores_without_any_llm_call(harness: Harness):
    harness.write_papers([paper("Mid", 3)])
    harness.clock.before_push()
    harness.run()
    calls = harness.llm_calls()

    def lift(data: dict) -> None:
        for tier in data["tiers"]:
            if tier["key"] == "must_read":
                tier["min_score"] = 55
            if tier["key"] == "digest":
                tier["min_score"] = 50

    harness.edit_rubric(lift)
    harness.rebuild()
    assert rescore(harness.ctx).ok

    assert harness.llm_calls() == calls
    record = json.loads(harness.archive_files()[0].read_text(encoding="utf-8"))
    assert record["assessments"]["alpha-topic"]["tier"] == "must_read"
    assert record["assessments"]["alpha-topic"]["score"] == 60


def test_rescore_that_drops_paper_removes_stale_archive(harness: Harness):
    harness.write_papers([paper("Mid", 3)])
    harness.run()
    assert harness.archive_files()

    def raise_bar(data: dict) -> None:
        for tier in data["tiers"]:
            if tier["key"] == "digest":
                tier["min_score"] = 90
            if tier["key"] == "must_read":
                tier["min_score"] = 95

    harness.edit_rubric(raise_bar)
    harness.rebuild()
    rescore(harness.ctx)
    assert harness.archive_files() == []


def test_semantic_change_never_bills_silently_and_old_scores_stop_ranking(harness: Harness):
    harness.clock.before_push()
    harness.write_papers([paper("Old Scale", 5)])
    harness.run()
    calls = harness.llm_calls("assess")

    harness.edit_rubric(lambda d: d["dimensions"][0]["anchors"].update({"3": "新定义"}))
    harness.rebuild()
    harness.clock.advance(hours=2)
    harness.run()

    assert harness.llm_calls("assess") == calls  # 不自动重评
    assert harness.pushed_titles() == []  # 旧标准的分数不参与排序
    # 改标准不能丢数据：归档保留，但标记为旧标准下的结果
    record = json.loads(harness.archive_files()[0].read_text(encoding="utf-8"))
    assert record["assessments"]["alpha-topic"]["stale"] is True


def test_reassess_dry_run_estimates_then_runs(harness: Harness):
    harness.clock.before_push()
    harness.write_papers([paper("Old Scale", 5)])
    harness.run()
    calls = harness.llm_calls("assess")

    harness.edit_rubric(lambda d: d["dimensions"][0]["anchors"].update({"3": "新定义"}))
    harness.rebuild()

    plan = reassess(harness.ctx, "alpha-topic", since=None, dry_run=True)
    assert plan.papers == 1
    assert plan.estimated_usd == pytest.approx(0.001)
    assert harness.llm_calls("assess") == calls

    harness.clock.advance(hours=2)
    reassess(harness.ctx, "alpha-topic", since=None, dry_run=False)
    harness.run()
    assert harness.llm_calls("assess") == calls + 1
    assert harness.pushed_titles() == ["Old Scale"]


def test_switch_topic_reuses_pdf_parse_and_extraction(harness: Harness):
    harness.write_papers([paper("Shared", 5)])
    harness.run()
    before = harness.ctx.store.stats()

    add_topic(harness, "beta-topic")
    harness.rebuild()
    pause_topic(harness.ctx, "alpha-topic")
    activate_topic(harness.ctx, "beta-topic")

    assert backfill_topic(harness.ctx, "beta-topic", since=None, dry_run=True).papers == 1
    assert harness.llm_calls("triage") == 1

    backfill_topic(harness.ctx, "beta-topic", since=None, dry_run=False)
    harness.clock.advance(minutes=5)
    harness.run()

    after = harness.ctx.store.stats()
    assert after["artifact_pdf"] == before["artifact_pdf"]
    assert after["extraction"] == before["extraction"]
    assert harness.llm_calls("extract") == 1
    assert harness.llm_calls("triage") == 2
    assert harness.llm_calls("assess") == 2
    assert after["assessment"] == 2


def test_new_topic_without_backfill_does_not_bill_history(harness: Harness):
    harness.write_papers([paper("History", 5)])
    harness.run()
    add_topic(harness, "beta-topic")
    harness.rebuild()
    harness.clock.advance(minutes=1)
    activate_topic(harness.ctx, "beta-topic")
    harness.clock.advance(minutes=1)
    harness.run()
    assert harness.llm_calls("triage") == 1


def test_paused_topic_consumes_nothing(harness: Harness):
    pause_topic(harness.ctx, "alpha-topic")
    harness.write_papers([paper("Ignored", 5)])
    harness.run()
    assert harness.llm_calls() == 0


def test_editing_topic_content_does_not_bill_history(harness: Harness):
    harness.clock.before_push()
    harness.write_papers([paper("Before Edit", 5)])
    harness.run()
    harness.edit_yaml("topics/alpha-topic.yaml", lambda d: d.update(content="new focus"))
    harness.rebuild()
    harness.clock.advance(hours=2)
    report = harness.run()

    assert harness.llm_calls("triage") == 1
    assert any("backfill" in warning for warning in report.warnings)
    assert harness.pushed_titles() == []  # 旧主题定义下的判断不再参与排序


def test_public_channel_only_gets_public_topics_and_whitelisted_fields(harness: Harness):
    add_topic(harness, "open-topic", public=True)
    harness.rebuild()
    activate_topic(harness.ctx, "open-topic")
    harness.write_papers([paper("Secret Abstract Paper", 5)])
    harness.clock.advance(minutes=1)
    harness.run()

    public = harness.publisher("web").sent
    assert len(public) == 1
    assert "Alpha" not in public[0].body  # alpha-topic 不公开
    assert "Abstract of" not in public[0].body
    assert harness.pushed_titles("web") == ["Secret Abstract Paper"]


def test_config_errors_are_reported_clearly(harness: Harness):
    harness.edit_yaml("topics/alpha-topic.yaml", lambda d: d.update(rubric="missing"))
    with pytest.raises(ConfigError, match="missing"):
        load_config(harness.root / "config")


def test_route_with_unknown_channel_or_tier_rejected(harness: Harness):
    harness.edit_yaml("channels.yaml", lambda d: d["routes"][0].update(channel="nope"))
    with pytest.raises(ConfigError, match="nope"):
        load_config(harness.root / "config")
    harness.edit_yaml(
        "channels.yaml",
        lambda d: d["routes"][0].update(channel="me", tiers=["typo_tier"]),
    )
    with pytest.raises(ConfigError, match="typo_tier"):
        load_config(harness.root / "config")


def test_invalid_rubric_stops_startup_with_filename(harness: Harness):
    harness.edit_rubric(lambda d: d["dimensions"][0].update(weight=1))
    with pytest.raises(ConfigError, match=r"default\.json"):
        load_config(harness.root / "config")


def test_unknown_adapter_rejected(harness: Harness):
    harness.edit_yaml("sources.yaml", lambda d: d["sources"]["alpha"].update(adapter="nope"))
    with pytest.raises(ConfigError, match="nope"):
        build_context(load_config(harness.root / "config"), clock=harness.clock)


def test_chunker_change_enters_fingerprint_but_never_bills_silently(harness: Harness):
    harness.clock.before_push()
    harness.write_papers([paper("Chunked", 5)])
    harness.run()
    (paper_id,) = harness.ctx.store.judgments.papers_with_scores()
    state = harness.ctx.store.papers.get_fulltext(paper_id)
    extraction = harness.ctx.store.papers.get_extraction(paper_id)
    assert state is not None and state.doc_key and extraction is not None
    assert extraction.input_hash == extraction_input_hash(
        state.doc_key, harness.ctx.settings.chunker
    )
    calls = (harness.llm_calls("extract"), harness.llm_calls("assess"))

    harness.edit_yaml("app.yaml", lambda d: d.update(chunker={"target_tokens": 400}))
    harness.rebuild()
    harness.clock.advance(minutes=5)
    harness.run()

    assert (harness.llm_calls("extract"), harness.llm_calls("assess")) == calls
    assert extraction.input_hash != extraction_input_hash(
        state.doc_key, harness.ctx.settings.chunker
    )
    # 指纹变了：显式 reassess 能找到这篇，说明切分版本已计入评估输入
    assert reassess(harness.ctx, "alpha-topic", since=None, dry_run=True).papers == 1


def test_paper_hit_by_two_topics_is_pushed_once_per_channel(harness: Harness):
    add_topic(harness, "beta-topic")
    harness.rebuild()
    activate_topic(harness.ctx, "beta-topic")
    harness.clock.before_push()
    harness.write_papers([paper("Shared Hit", 5)])
    harness.clock.advance(hours=1)
    harness.run()

    assert harness.llm_calls("assess") == 2  # 两个主题各自评估
    assert harness.pushed_titles() == ["Shared Hit"]


def test_chunker_change_does_not_revive_dead_extract_jobs(harness: Harness):
    harness.write_papers([paper("Dead Extract", 5)])
    harness.llm.fail_tasks["extract"] = [TransientError("x") for _ in range(3)]
    for _ in range(3):
        harness.run()
        harness.clock.advance(hours=1)
    calls = harness.llm_calls("extract")
    assert [job.stage for job in harness.ctx.store.jobs.list_by_status("dead")] == ["extract"]

    harness.edit_yaml("app.yaml", lambda d: d.update(chunker={"target_tokens": 400}))
    harness.rebuild()
    harness.run()

    assert harness.llm_calls("extract") == calls


def test_glossary_file_is_loaded_and_not_mistaken_for_a_topic(harness: Harness):
    path = harness.root / "config" / "topics" / "alpha-topic.glossary.yaml"
    path.write_text(
        yaml.safe_dump({"terms": [{"en": "self-attention", "zh": "自注意力"}]}, allow_unicode=True),
        encoding="utf-8",
    )
    ctx = harness.rebuild()
    assert set(ctx.topics) == {"alpha-topic"}
    assert [t.zh for t in ctx.glossaries["alpha-topic"].terms] == ["自注意力"]


def test_glossary_for_unknown_topic_is_rejected(harness: Harness):
    path = harness.root / "config" / "topics" / "ghost.glossary.yaml"
    path.write_text(yaml.safe_dump({"terms": []}), encoding="utf-8")
    with pytest.raises(ConfigError, match="ghost"):
        load_config(harness.root / "config")
