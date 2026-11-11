"""全流程测试环境：真实配置加载、真实数据库和迁移，外部依赖全部是假适配器。"""

import json
import shutil
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
import yaml

from litgleaner.application.context import AppContext
from litgleaner.application.pipeline import RunReport, run_pipeline
from litgleaner.application.topics import activate_topic
from litgleaner.bootstrap import build_context
from litgleaner.infra.config import load_config
from litgleaner.infra.db.migrate import upgrade_database

ROOT = Path(__file__).parents[2]


def extraction_answer(**changes) -> dict:
    """明确构造当前提取协议的离线回答；不修补网关收到的旧协议响应。"""
    return {
        "summary": None,
        "conclusion": None,
        "innovation": None,
        "key_concepts": [],
        "limitations": [],
        "problem": None,
        "prior_gap": None,
        "approach": None,
        "claims": [],
        "method": None,
        "datasets": [],
        "metrics": [],
        "results": [],
        "code_data": [],
        "questions": [
            {"question": question, "answer": None}
            for question in ("研究问题是什么？", "方法有什么依据？", "结果适用何种条件？")
        ],
        "highlights": [],
        **changes,
    }


def paper(title: str, level: int | None = None, marker: str = "", **kw) -> dict:
    tag = f" [[level:{level}]]" if level is not None else ""
    return {
        "title": title,
        "abstract": f"Abstract of {title}. Research on alpha and beta.{tag}{marker}",
        "authors": [f"Author {title}"],
        "doi": f"10.1000/{title.lower().replace(' ', '-')}",
        "published_at": "2026-10-01",
        **kw,
    }


class Clock:
    def __init__(self) -> None:
        self.now = datetime(2026, 11, 3, 1, 0, tzinfo=UTC)

    def __call__(self) -> datetime:
        return self.now

    def advance(self, **kw: float) -> None:
        self.now += timedelta(**kw)

    def before_push(self) -> None:
        """前进到次日上海时间 08:00，还没到 08:30 的推送时间。时钟只向前走。"""
        self.now = self.now.replace(hour=0, minute=0) + timedelta(days=1)


@dataclass
class Harness:
    root: Path
    clock: Clock
    ctx: AppContext

    def rebuild(self) -> AppContext:
        """重新加载配置，等同于改完配置文件后再次运行程序。"""
        self.ctx = build_context(load_config(self.root / "config"), clock=self.clock)
        return self.ctx

    def run(self, only: str | None = None) -> RunReport:
        return run_pipeline(self.ctx, only=only)

    def write_papers(self, papers: list[dict], name: str = "papers.json") -> None:
        (self.root / "config" / "demo" / name).write_text(json.dumps(papers), encoding="utf-8")

    def edit_yaml(self, relative: str, change: Callable[[dict], None]) -> None:
        path = self.root / "config" / relative
        data = yaml.safe_load(path.read_text(encoding="utf-8"))
        change(data)
        path.write_text(yaml.safe_dump(data, allow_unicode=True), encoding="utf-8")

    def edit_rubric(self, change: Callable[[dict], None]) -> None:
        path = self.root / "config" / "rubrics" / "default.json"
        data = json.loads(path.read_text(encoding="utf-8"))
        change(data)
        path.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")

    @property
    def llm(self):
        return self.ctx.llm.routes["triage"].llm

    def llm_calls(self, task: str | None = None) -> int:
        return self.ctx.store.runs.llm_call_count(task)

    def publisher(self, name: str = "me"):
        return self.ctx.channels[name].publisher

    def pushed_titles(self, name: str = "me") -> list[str]:
        return [
            line.split("] ", 1)[1]
            for sent in self.publisher(name).sent
            for line in sent.body.splitlines()
            if line.startswith("[")
        ]

    def archive_files(self) -> list[Path]:
        return sorted((self.root / "archive").rglob("*.json"))


def _write_config(root: Path) -> None:
    config = root / "config"
    (config / "rubrics").mkdir(parents=True)
    (config / "topics").mkdir()
    (config / "journals").mkdir()
    (config / "demo").mkdir()
    shutil.copy(ROOT / "config" / "rubrics" / "default.json", config / "rubrics" / "default.json")
    shutil.copytree(ROOT / "prompts", root / "prompts")
    files = {
        "app.yaml": {
            "database": "data/db/litgleaner.db",
            "blob_root": "data/blobs",
            "archive_dir": "archive",
            "prompts_dir": "prompts",
            "timezone": "Asia/Shanghai",
            "language": {"ui": "zh-CN", "reading": "zh-CN"},
            "jobs": {"max_attempts": 3, "backoff_seconds": 300},
            "assess": {"token_budget": 6000},
            "fulltext": [{"adapter": "fake_fulltext"}],
            "parser": {"adapter": "fake_parser"},
        },
        "sources.yaml": {
            "sources": {
                "alpha": {
                    "adapter": "fake_source",
                    "options": {"records_file": "config/demo/papers.json"},
                }
            }
        },
        "channels.yaml": {
            "channels": {
                "me": {
                    "adapter": "fake_publisher",
                    "renderer": "plain_text",
                    "visibility": "private",
                },
                "web": {
                    "adapter": "fake_publisher",
                    "renderer": "plain_text",
                    "visibility": "public",
                },
            },
            "routes": [
                {
                    "id": "daily-me",
                    "topic": "*",
                    "channel": "me",
                    "schedule": "daily 08:30",
                    "tiers": ["must_read", "digest", "need_fulltext"],
                    "limit": 5,
                },
                {
                    "id": "daily-web",
                    "topic": "*",
                    "channel": "web",
                    "schedule": "daily 08:30",
                    "tiers": ["must_read", "digest"],
                    "limit": 5,
                },
            ],
        },
        "llm.yaml": {
            "providers": {"main": {"adapter": "fake_llm"}},
            "tasks": {
                "triage": {"provider": "main", "model": "cheap", "prompt": "triage"},
                "extract": {"provider": "main", "model": "strong", "prompt": "extract"},
                "assess": {"provider": "main", "model": "strong", "prompt": "assess"},
                "translate": {"provider": "main", "model": "cheap", "prompt": "translate"},
            },
            "budget": {"daily_usd": 10.0},
        },
        "journals/core.yaml": {"issns": ["1234-5678"]},
        "topics/alpha-topic.yaml": {
            "id": "alpha-topic",
            "name": "Alpha",
            "background": "alpha background",
            "content": "alpha research",
            "goals": [{"id": "g1", "text": "alpha goal"}],
            "query": {"include": ["alpha"]},
            "journal_sets": ["core"],
            "rubric": "default",
        },
    }
    for relative, data in files.items():
        (config / relative).write_text(yaml.safe_dump(data, allow_unicode=True), encoding="utf-8")
    (config / "demo" / "papers.json").write_text("[]", encoding="utf-8")


@pytest.fixture
def harness(tmp_path: Path) -> Harness:
    _write_config(tmp_path)
    clock = Clock()
    config = load_config(tmp_path / "config")
    upgrade_database(config.database_url)
    ctx = build_context(config, clock=clock)
    activate_topic(ctx, "alpha-topic")
    return Harness(tmp_path, clock, ctx)


__all__ = ["Harness", "paper"]
