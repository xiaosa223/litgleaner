"""从假信号源到假渠道的全流程，覆盖漏斗、幂等、失败隔离和预算。"""

import json

from litgleaner.domain.errors import AuthFailed, TransientError
from tests.e2e.conftest import Harness, paper


def standard_papers() -> list[dict]:
    return [
        paper("Top Paper", 5),
        paper("Good Paper", 3),
        paper("Weak Paper", 1),
        paper("Offtopic Paper", 5, " [[irrelevant]]"),
        paper("NoPDF Paper", 4),
    ]


def test_full_flow_ranks_filters_archives_and_pushes(harness: Harness):
    harness.write_papers(standard_papers())
    report = harness.run()

    assert report.ok, report.summary()
    assert harness.pushed_titles() == ["Top Paper", "NoPDF Paper", "Good Paper"]
    sent = harness.publisher().sent[0].body
    assert "100" in sent
    assert "We found it works." not in sent or "未核实" not in sent

    archived = {json.loads(p.read_text(encoding="utf-8"))["title"] for p in harness.archive_files()}
    assert archived == {"Top Paper", "Good Paper", "NoPDF Paper"}


def test_funnel_rejected_paper_is_never_downloaded_or_assessed(harness: Harness):
    harness.write_papers(standard_papers())
    harness.run()
    stats = harness.ctx.store.stats()
    assert stats["paper"] == 5
    assert stats["triage"] == 5
    assert stats["artifact_pdf"] == 3  # 不相关和无 PDF 的论文都没有 PDF
    assert stats["assessment"] == 4
    assert harness.llm_calls("triage") == 5
    assert harness.llm_calls("assess") == 4


def test_abstract_only_paper_is_provisional_and_nulls_fulltext_dimensions(harness: Harness):
    harness.write_papers([paper("NoPDF Paper", 5)])
    harness.run()
    record = json.loads(harness.archive_files()[0].read_text(encoding="utf-8"))
    assessment = record["assessments"]["alpha-topic"]
    assert assessment["fulltext_used"] is False
    assert assessment["provisional"] is True
    assert assessment["tier"] == "need_fulltext"
    assert assessment["levels"]["evidence"] is None
    assert assessment["levels"]["relevance"] == 5


def test_rerun_is_idempotent_no_extra_llm_no_second_push(harness: Harness):
    harness.write_papers(standard_papers())
    harness.run()
    calls = harness.llm_calls()
    pushes = len(harness.publisher().sent)
    snapshot = harness.ctx.store.stats()

    assert harness.run().ok
    harness.clock.advance(minutes=30)
    assert harness.run().ok

    assert harness.llm_calls() == calls
    assert len(harness.publisher().sent) == pushes
    assert harness.ctx.store.stats() == snapshot


def test_next_day_only_new_papers_are_processed_and_pushed(harness: Harness):
    harness.write_papers([paper("Day One", 5)])
    harness.run()
    harness.clock.advance(days=1)
    harness.write_papers([paper("Day One", 5), paper("Day Two", 5)])
    harness.run()
    assert harness.pushed_titles() == ["Day One", "Day Two"]
    assert harness.ctx.store.stats()["paper"] == 2


def test_push_waits_for_schedule(harness: Harness):
    harness.clock.before_push()
    harness.write_papers([paper("Early", 5)])
    harness.run()
    assert harness.publisher().sent == []
    harness.clock.advance(minutes=31)
    harness.run()
    assert harness.pushed_titles() == ["Early"]


def test_same_doi_from_second_record_is_merged(harness: Harness):
    original = paper("Same Paper", 5)
    harness.write_papers([original, {**original, "title": "Same Paper (published version)"}])
    harness.run()
    assert harness.ctx.store.stats()["paper"] == 1


def test_later_record_fills_missing_abstract(harness: Harness):
    base = paper("Sparse", 5)
    harness.write_papers([{**base, "abstract": None}, base])
    harness.run()
    assert harness.ctx.store.stats()["paper"] == 1
    assert harness.archive_files()


def test_malformed_record_fails_that_job_without_stopping_pipeline(harness: Harness):
    harness.write_papers([{"title": "   "}])
    report = harness.run()
    assert not report.ok
    assert harness.ctx.store.stats()["paper"] == 0


def test_auth_failure_is_isolated_dead_and_reported(harness: Harness):
    harness.write_papers([paper("Any", 5)])
    harness.ctx.sources["alpha"].fail_next.append(AuthFailed("token expired"))
    report = harness.run()
    assert not report.ok
    assert "token expired" in report.summary()
    dead = harness.ctx.store.jobs.list_by_status("dead")
    assert len(dead) == 1
    assert dead[0].attempts == 1


def test_transient_llm_failure_retries_after_backoff(harness: Harness):
    harness.write_papers([paper("Flaky", 5)])
    harness.llm.fail_next.append(TransientError("503"))
    first = harness.run()
    assert not first.ok
    assert harness.pushed_titles() == []

    harness.run()  # 退避时间未到，不会重试
    assert harness.pushed_titles() == []

    harness.clock.advance(seconds=301)
    assert harness.run().ok
    assert harness.pushed_titles() == ["Flaky"]


def test_repeated_failure_becomes_dead_after_max_attempts(harness: Harness):
    harness.write_papers([paper("Broken", 5)])
    harness.llm.fail_next.extend(TransientError("x") for _ in range(3))
    for _ in range(3):
        harness.run()
        harness.clock.advance(hours=1)
    dead = harness.ctx.store.jobs.list_by_status("dead")
    assert [job.stage for job in dead] == ["triage"]
    assert dead[0].attempts == 3


def test_invalid_llm_output_is_rejected_including_invented_total_score(harness: Harness):
    harness.write_papers([paper("Injected", 5)])
    harness.llm.overrides["assess"] = {"dimensions": {}, "total_score": 100}
    report = harness.run()
    assert not report.ok
    assert harness.ctx.store.stats()["assessment"] == 0
    assert harness.pushed_titles() == []


def test_parse_failure_falls_back_to_abstract_only(harness: Harness):
    harness.write_papers([paper("BadParse Paper", 5)])
    report = harness.run()
    assert not report.ok  # 死信必须被报告
    record = json.loads(harness.archive_files()[0].read_text(encoding="utf-8"))
    assert record["assessments"]["alpha-topic"]["fulltext_used"] is False


def test_publisher_failure_retries_and_delivers_exactly_once(harness: Harness):
    harness.write_papers([paper("Once", 5)])
    harness.publisher().fail_next.append(TransientError("telegram down"))
    assert not harness.run().ok
    assert harness.publisher().sent == []
    harness.clock.advance(seconds=301)
    assert harness.run().ok
    assert len(harness.publisher().sent) == 1
    harness.clock.advance(seconds=301)
    harness.run()
    assert len(harness.publisher().sent) == 1


def test_crash_during_send_is_not_resent_automatically(harness: Harness):
    """发送结果未知时宁可告警，也不能重复推送。"""
    harness.write_papers([paper("Unknown", 5)])
    harness.publisher().crash_after_send = True
    report = harness.run()
    assert not report.ok
    harness.publisher().crash_after_send = False
    harness.clock.advance(hours=1)
    report = harness.run()
    assert len(harness.publisher().sent) == 1
    assert not report.ok
    assert "未知" in report.summary()


def test_budget_stops_llm_without_counting_failures_and_resumes_next_day(harness: Harness):
    harness.edit_yaml("llm.yaml", lambda d: d["budget"].update(daily_usd=0.0025))
    harness.rebuild()
    harness.write_papers([paper(f"Paper {i}", 5) for i in range(5)])
    report = harness.run()
    assert report.budget_stopped
    assert harness.llm_calls("triage") == 3  # 花到超预算为止，每次 0.001
    assert harness.ctx.store.jobs.list_by_status("dead") == ()
    assert all(job.attempts == 0 for job in harness.ctx.store.jobs.list_by_status("pending"))

    harness.clock.advance(days=1)
    harness.run()
    assert harness.ctx.store.stats()["triage"] >= 5


def test_archive_is_deterministic_on_rebuild(harness: Harness):
    harness.write_papers([paper("Stable", 5)])
    harness.run()
    path = harness.archive_files()[0]
    before = path.read_text(encoding="utf-8")
    harness.run()
    assert path.read_text(encoding="utf-8") == before


def test_quote_verification_marks_fabricated_evidence(harness: Harness):
    harness.write_papers([paper("Quoted", 5)])
    harness.llm.overrides["extract"] = {
        "summary": "s",
        "key_concepts": [],
        "conclusion": "c",
        "innovation": None,
        "limitations": [
            {"text": "真", "source": "author", "quote": "We found it works."},
            {"text": "假", "source": "author", "quote": "This sentence was invented."},
        ],
    }
    harness.run()
    record = json.loads(harness.archive_files()[0].read_text(encoding="utf-8"))
    verified = {item["text"]: item["verified"] for item in record["extraction"]["limitations"]}
    assert verified == {"真": True, "假": False}
    assert "假（未核实）" in harness.publisher().sent[0].body


def _assess_override(harness: Harness, **levels: int | None) -> None:
    keys = ("relevance", "novelty", "evidence", "reusability")
    harness.llm.overrides["assess"] = {
        "dimensions": {
            key: {"level": levels.get(key, 5), "reason": "演示理由", "quote": None} for key in keys
        }
    }


def test_undetermined_dimension_is_marked_but_not_capped(harness: Harness):
    harness.clock.before_push()
    harness.write_papers([paper("Unclear Evidence", 5)])
    _assess_override(harness, evidence=None)
    harness.clock.advance(hours=1)
    harness.run()

    record = json.loads(harness.archive_files()[0].read_text(encoding="utf-8"))
    assessment = record["assessments"]["alpha-topic"]
    assert assessment["fulltext_used"] is True
    assert assessment["provisional"] is False
    assert assessment["undetermined"] == ["evidence"]
    assert assessment["tier"] == "must_read"
    body = harness.publisher().sent[0].body
    assert "无法判断：证据可信度" in body
    assert "只读了摘要" not in body


def test_empty_gate_dimension_is_not_pushed(harness: Harness):
    harness.clock.before_push()
    harness.write_papers([paper("Unknown Relevance", 5)])
    _assess_override(harness, relevance=None)
    harness.clock.advance(hours=1)
    harness.run()

    assert harness.pushed_titles() == []


def test_provisional_push_is_repeated_once_after_fulltext_upgrade(harness: Harness):
    harness.clock.before_push()
    harness.clock.advance(hours=1)
    harness.write_papers([paper("Late Fulltext", 5)])
    harness.llm.fail_tasks["extract"] = [TransientError("x") for _ in range(3)]
    for _ in range(3):
        harness.run()
        harness.clock.advance(hours=1)
    assert harness.pushed_titles() == ["Late Fulltext"]
    assert "只读了摘要" in harness.publisher().sent[0].body

    harness.ctx.store.jobs.retry_dead(harness.ctx.now_iso())
    harness.run()  # 全文提取成功，按全文重新评估，升级为必读
    harness.clock.before_push()
    harness.clock.advance(hours=1)
    harness.run()

    assert harness.pushed_titles() == ["Late Fulltext", "Late Fulltext"]
    upgraded = harness.publisher().sent[1].body
    assert "必读" in upgraded
    assert "读完全文后升级" in upgraded

    harness.clock.before_push()
    harness.clock.advance(hours=1)
    harness.run()
    assert len(harness.pushed_titles()) == 2  # 只补推一次
