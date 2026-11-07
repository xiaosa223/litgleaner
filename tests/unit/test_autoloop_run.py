"""scripts/autoloop_run.py 把拾穗的运行结果翻译成 agent-autoloop 回执。

防的失败方式：
- 正常跑完也去打扰人，或者同一个问题每天重复报告；
- 失败被说成成功、或者把要人判断的事写成“没跑成”导致无限重试；
- 回答被映射错（选了“没收到”却标记成已发送）。
"""

from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path

import pytest

pytestmark = pytest.mark.unit

_SCRIPT = Path(__file__).resolve().parents[2] / "scripts" / "autoloop_run.py"
_spec = importlib.util.spec_from_file_location("autoloop_run", _SCRIPT)
assert _spec and _spec.loader
autoloop_run = importlib.util.module_from_spec(_spec)
sys.modules["autoloop_run"] = autoloop_run
_spec.loader.exec_module(autoloop_run)

Cmd = autoloop_run.Cmd
OK_SUMMARY = "discover: 完成 2，待重试 0，死信 0\npublish: 完成 1，待重试 0，死信 0"


def receipt_of(message: str) -> dict | None:
    if "```agent-autoloop" not in message:
        return None
    block = message.split("```agent-autoloop", 1)[1].split("```", 1)[0]
    return json.loads(block)


def first_line(message: str) -> str:
    return message.strip().splitlines()[0]


def test_parse_jobs_and_deliveries_ignore_count_line():
    jobs = autoloop_run.parse_jobs("parse\t3\tpaper:1\tTimeout\nextract\t3\tpaper:2\t\n共 2 个\n")
    assert jobs == [
        autoloop_run.DeadJob("parse", 3, "paper:1", "Timeout"),
        autoloop_run.DeadJob("extract", 3, "paper:2", ""),
    ]
    assert autoloop_run.parse_deliveries("D1\ttelegram\n共 1 个\n") == [
        autoloop_run.Delivery("D1", "telegram")
    ]
    assert autoloop_run.parse_jobs("共 0 个\n") == []


def test_clean_run_stays_quiet():
    message = autoloop_run.daily_message(Cmd(0, OK_SUMMARY, ""), [], [])
    assert first_line(message).startswith("拾穗跑完")
    assert receipt_of(message) is None


def test_locked_run_is_nothing_to_do():
    message = autoloop_run.daily_message(Cmd(3, "", "已有流程正在运行，本次退出"), [], [])
    assert first_line(message).startswith("没事可做")


def test_pending_retries_without_dead_letters_stay_quiet():
    message = autoloop_run.daily_message(Cmd(1, "parse: 完成 0，待重试 2，死信 0", ""), [], [])
    assert receipt_of(message) is None
    assert "待重试" in first_line(message)


def test_dead_letters_emit_one_stable_triage_event():
    dead = [
        autoloop_run.DeadJob("parse", 3, "paper:2", "Timeout"),
        autoloop_run.DeadJob("parse", 3, "paper:1", "Timeout"),
    ]
    first = receipt_of(autoloop_run.daily_message(Cmd(1, OK_SUMMARY, ""), dead, []))
    again = receipt_of(autoloop_run.daily_message(Cmd(1, OK_SUMMARY, ""), list(reversed(dead)), []))
    assert first and again
    [event] = first["events"]
    assert event["type"] == "litfactory.failed"
    assert event["id"] == again["events"][0]["id"], "同一批死信每天重报会让排障重复跑"
    assert "paper:1" in event["detail"] and "Timeout" in event["detail"]
    assert "ask" not in first and "notice" not in first, "诊断由排障规则给出，这里不重复发卡"


def test_start_failure_is_reported_not_retried():
    error = "数据库不是最新版本，请先执行：litgleaner db upgrade"
    message = autoloop_run.daily_message(Cmd(2, "", error), [], [])
    assert not first_line(message).startswith("没跑成"), "配置错误重试也不会好"
    receipt = receipt_of(message)
    assert receipt
    [event] = receipt["events"]
    assert event["type"] == "litfactory.failed"
    assert "db upgrade" in event["detail"]


def test_unknown_delivery_asks_with_machine_readable_about():
    unknown = [autoloop_run.Delivery("D1", "telegram")]
    receipt = receipt_of(autoloop_run.daily_message(Cmd(0, OK_SUMMARY, ""), [], unknown))
    assert receipt
    [ask] = receipt["ask"]
    assert ask["key"] == "delivery:D1:telegram"
    assert len(ask["options"]) == 2
    assert json.loads(ask["about"]) == {"delivery": ["D1", "telegram"]}


def test_answers_resolve_deliveries_with_the_chosen_flag():
    calls: list[tuple[str, str, bool]] = []

    def resolve(digest: str, channel: str, resend: bool) -> Cmd:
        calls.append((digest, channel, resend))
        return Cmd(0, "已处理", "")

    answers = [
        {"about": json.dumps({"delivery": ["D1", "telegram"]}), "choice": 0},
        {"about": json.dumps({"delivery": ["D2", "web"]}), "choice": 1},
    ]
    message = autoloop_run.answers_message(answers, resolve)
    assert calls == [("D1", "telegram", False), ("D2", "web", True)]
    assert "2" in first_line(message)


def test_free_text_answer_is_not_guessed():
    calls: list[tuple[str, str, bool]] = []

    def resolve(digest: str, channel: str, resend: bool) -> Cmd:
        calls.append((digest, channel, resend))
        return Cmd(0, "", "")

    about = json.dumps({"delivery": ["D1", "telegram"]})
    answers = [{"about": about, "choice": None, "text": "不确定"}]
    message = autoloop_run.answers_message(answers, resolve)
    assert calls == []
    receipt = receipt_of(message)
    assert receipt and receipt["notice"][0]["fyi"] is True


def test_resolve_failure_is_retried():
    def resolve(digest: str, channel: str, resend: bool) -> Cmd:
        return Cmd(1, "", "database is locked")

    answers = [{"about": json.dumps({"delivery": ["D1", "telegram"]}), "choice": 1}]
    assert first_line(autoloop_run.answers_message(answers, resolve)).startswith("没跑成")
