"""agent-autoloop 的接单脚本：跑一遍拾穗，把结果翻译成回执。

agent-autoloop 把这个脚本当作一个 `command` Agent 启动。它不调用大模型，0 Token：
- 正常跑完或只有待重试：只写一句结论，不打扰人；
- 有死信或启动失败：发一条 `litfactory.failed` 事件，交给排障规则诊断。
  事件编号由死信内容算出，同一批问题只排障一次；
- 推送结果未知：发问题卡片，回答回来时执行 `delivery resolve`。

回执格式见 agent-autoloop 的 docs/protocol.md。用法：
    .venv/bin/python scripts/autoloop_run.py
上下文文件路径由 agent-autoloop 通过 AGENT_AUTOLOOP_CONTEXT 传入，不存在时按每日运行处理。
"""

from __future__ import annotations

import hashlib
import json
import os
import subprocess
import sys
from collections.abc import Callable, Iterable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
DETAIL_LIMIT = 3800
OPTIONS = ["收到了（标记为已发送）", "没收到（下次运行重发）"]


@dataclass(frozen=True)
class Cmd:
    code: int
    stdout: str
    stderr: str


@dataclass(frozen=True)
class DeadJob:
    stage: str
    attempts: int
    key: str
    error: str


@dataclass(frozen=True)
class Delivery:
    digest: str
    channel: str


def _rows(stdout: str) -> Iterable[list[str]]:
    for line in stdout.splitlines():
        if "\t" in line:
            yield line.split("\t")


def parse_jobs(stdout: str) -> list[DeadJob]:
    jobs = []
    for parts in _rows(stdout):
        stage, attempts, key, *error = parts
        jobs.append(DeadJob(stage, int(attempts), key, "\t".join(error)))
    return jobs


def parse_deliveries(stdout: str) -> list[Delivery]:
    return [Delivery(parts[0], parts[1]) for parts in _rows(stdout) if len(parts) >= 2]


def _fingerprint(*parts: str) -> str:
    return hashlib.sha256("\n".join(parts).encode()).hexdigest()[:16]


def _tail(text: str, limit: int = DETAIL_LIMIT) -> str:
    text = text.strip()
    return text if len(text) <= limit else "…" + text[-limit:]


def _message(first: str, receipt: dict[str, Any]) -> str:
    if not receipt:
        return first + "\n"
    block = json.dumps(receipt, ensure_ascii=False, indent=2)
    return f"{first}\n\n```agent-autoloop\n{block}\n```\n"


def _failed_event(kind: str, fingerprint: str, summary: str, detail: str) -> dict[str, str]:
    return {
        "id": f"{kind}:{fingerprint}",
        "type": "litfactory.failed",
        "subject": "daily",
        "summary": summary[:200],
        "detail": _tail(detail),
    }


def _ask_delivery(item: Delivery) -> dict[str, Any]:
    return {
        "title": f"推送结果未知：{item.digest} → {item.channel}",
        "text": f"拾穗发出这份推送后没拿到确认。请到 {item.channel} 看一眼有没有收到。",
        "options": OPTIONS,
        "key": f"delivery:{item.digest}:{item.channel}",
        "about": json.dumps({"delivery": [item.digest, item.channel]}),
    }


def daily_message(run: Cmd, dead: list[DeadJob], unknown: list[Delivery]) -> str:
    """一次每日运行的最后一条消息：第一行是结局，有事要中台做时附回执。"""
    if run.code == 3:
        return "没事可做：上一轮拾穗还在运行\n"
    receipt: dict[str, Any] = {}
    if run.code not in (0, 1):
        error = (run.stderr or run.stdout).strip()
        first_error = error.splitlines()[0] if error else f"退出码 {run.code}"
        receipt["events"] = [
            _failed_event(
                "start",
                _fingerprint(str(run.code), first_error),
                f"拾穗没能运行：{first_error}",
                f"litgleaner run 退出码 {run.code}\n\n{error}",
            )
        ]
        first = f"拾穗没能运行，已交给排障：{first_error}"
    elif dead:
        ordered = sorted(dead, key=lambda job: (job.stage, job.key))
        lines = [f"{job.stage}\t{job.attempts}\t{job.key}\t{job.error}" for job in ordered]
        receipt["events"] = [
            _failed_event(
                "dead",
                _fingerprint(*(f"{job.stage}:{job.key}" for job in ordered)),
                f"拾穗有 {len(dead)} 个死信任务",
                "死信（阶段、尝试次数、任务、最后错误）：\n"
                + "\n".join(lines)
                + f"\n\n本次运行输出：\n{run.stdout.strip()}",
            )
        ]
        first = f"拾穗跑完，有 {len(dead)} 个死信，已交给排障"
    elif run.code == 1:
        first = "拾穗跑完，有任务待重试，下次运行自动重试"
    else:
        first = "拾穗跑完，没有异常"
    if unknown:
        receipt["ask"] = [_ask_delivery(item) for item in unknown]
    return _message(first, receipt)


def _delivery_of(answer: dict[str, Any]) -> Delivery | None:
    try:
        about = json.loads(answer.get("about") or "")
        digest, channel = about["delivery"]
    except (ValueError, KeyError, TypeError):
        return None
    return Delivery(str(digest), str(channel))


def answers_message(answers: list[dict[str, Any]], resolve: Callable[[str, str, bool], Cmd]) -> str:
    """回答回来的一轮：按选项执行 delivery resolve，不重跑流程。"""
    done = 0
    notices = []
    for answer in answers:
        item = _delivery_of(answer)
        if item is None:
            continue
        choice = answer.get("choice")
        if choice not in (0, 1):
            notices.append(
                {
                    "title": f"没有处理推送 {item.digest} → {item.channel}",
                    "text": "回答里没有选项，没法判断是否收到。下次运行会重新问你，请点选项回答。",
                    "key": f"unanswered:{item.digest}:{item.channel}",
                    "fyi": True,
                }
            )
            continue
        result = resolve(item.digest, item.channel, choice == 1)
        if result.code != 0:
            lines = (result.stderr or result.stdout).strip().splitlines()
            reason = lines[0] if lines else f"退出码 {result.code}"
            return f"没跑成：处理推送 {item.digest} 时出错：{reason}\n"
        done += 1
    receipt = {"notice": notices} if notices else {}
    return _message(f"已按你的选择处理 {done} 份推送", receipt)


def _litgleaner(*args: str) -> Cmd:
    program = Path(sys.executable).parent / "litgleaner"
    completed = subprocess.run(
        [str(program), *args], cwd=ROOT, capture_output=True, text=True, check=False
    )
    return Cmd(completed.returncode, completed.stdout, completed.stderr)


def _resolve(digest: str, channel: str, resend: bool) -> Cmd:
    return _litgleaner("delivery", "resolve", digest, channel, "--resend" if resend else "--sent")


def _context() -> dict[str, Any]:
    path = os.environ.get("AGENT_AUTOLOOP_CONTEXT")
    if not path:
        return {}
    return dict(json.loads(Path(path).read_text(encoding="utf-8")))


def main() -> int:
    answers = [a for a in _context().get("answers", []) if _delivery_of(a) is not None]
    if answers:
        print(answers_message(answers, _resolve), end="")
        return 0
    run = _litgleaner("run")
    dead = parse_jobs(_litgleaner("jobs", "list", "--status", "dead").stdout)
    unknown = parse_deliveries(_litgleaner("delivery", "list").stdout)
    print(daily_message(run, dead, unknown), end="")
    return 0


if __name__ == "__main__":
    sys.exit(main())
