"""组装程序：配置里的适配器名称在这里变成具体实现。新增适配器只改注册表。"""

import os
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from litfactory.adapters.blobstore.local import LocalBlobStore
from litfactory.adapters.fakes.fulltext import FakeFullText
from litfactory.adapters.fakes.llm import FakeLLM
from litfactory.adapters.fakes.parser import FakeParser
from litfactory.adapters.fakes.publisher import FakePublisher
from litfactory.adapters.fakes.source import FakeSource
from litfactory.adapters.parsers.mineru_cloud import MinerUCloudParser
from litfactory.adapters.parsers.pdf_text import PdfTextParser
from litfactory.adapters.renderers.plain_text import PlainTextRenderer
from litfactory.application.context import AppContext, ChannelBinding, RuntimeSettings
from litfactory.application.llm_gateway import LLMGateway, Prompt, TaskRoute
from litfactory.domain.chunker import ChunkerConfig
from litfactory.domain.errors import ConfigError
from litfactory.infra.config import AdapterConfig, LoadedConfig
from litfactory.infra.db.store import SqlStore
from litfactory.ports.fulltext import FullTextProvider
from litfactory.ports.llm import LLM
from litfactory.ports.parser import Parser
from litfactory.ports.publisher import Publisher, Renderer
from litfactory.ports.source import Source


@dataclass(frozen=True)
class BuildArgs:
    name: str
    options: Mapping[str, Any]
    root: Path
    credentials: Mapping[str, str]

    def secret(self, env_name: str) -> str:
        """密钥只从环境变量读取，配置文件里只写变量名。"""
        if env_name not in self.credentials:
            raise ConfigError(f"{self.name} 需要在 credentials_env 中声明 {env_name}")
        return os.environ.get(env_name, "")

    def options_with_paths(self, *keys: str) -> dict[str, Any]:
        """把配置里的相对路径换成项目根目录下的绝对路径。"""
        resolved = dict(self.options)
        for key in keys:
            if key in resolved:
                resolved[key] = str(self.root / str(resolved[key]))
        return resolved

    def path(self, option: str) -> Path:
        return self.root / str(self.options[option])

    def optional_path(self, option: str) -> Path | None:
        return self.path(option) if option in self.options else None


SOURCES: Mapping[str, Callable[[BuildArgs], Source]] = {
    "fake_source": lambda a: FakeSource(a.name, a.path("records_file")),
}
FULLTEXT: Mapping[str, Callable[[BuildArgs], FullTextProvider]] = {
    "fake_fulltext": lambda _: FakeFullText(),
}
PARSERS: Mapping[str, Callable[[BuildArgs], Parser]] = {
    "fake_parser": lambda _: FakeParser(),
    "pdf_text": lambda _: PdfTextParser(),
    "mineru_cloud": lambda a: MinerUCloudParser(
        a.secret("MINERU_API_TOKEN"), a.options_with_paths("state_dir")
    ),
}
LLMS: Mapping[str, Callable[[BuildArgs], LLM]] = {
    "fake_llm": lambda _: FakeLLM(),
}
PUBLISHERS: Mapping[str, Callable[[BuildArgs], Publisher]] = {
    "fake_publisher": lambda a: FakePublisher(a.optional_path("output_file")),
}
RENDERERS: Mapping[str, Callable[[], Renderer]] = {
    "plain_text": PlainTextRenderer,
}


def _pick[T](registry: Mapping[str, T], kind: str, name: str) -> T:
    if name not in registry:
        raise ConfigError(f"未知的{kind}适配器：{name}，可选：{sorted(registry)}")
    return registry[name]


def _build[T](
    registry: Mapping[str, Callable[[BuildArgs], T]],
    kind: str,
    name: str,
    config: AdapterConfig,
    root: Path,
) -> T:
    factory = _pick(registry, kind, config.adapter)
    credentials = {env: env for env in config.credentials_env}
    return factory(BuildArgs(name, config.options, root, credentials))


def _load_prompts(config: LoadedConfig) -> dict[str, Prompt]:
    directory = config.path(config.app.prompts_dir)
    names = {task.prompt for task in config.llm.tasks.values()}
    prompts: dict[str, Prompt] = {}
    for name in names:
        path = directory / f"{name}.md"
        if not path.is_file():
            raise ConfigError(f"找不到提示词文件：{path}")
        prompts[name] = Prompt(name, path.read_text(encoding="utf-8"))
    return prompts


def _build_llm(config: LoadedConfig, store: SqlStore, clock: Callable[[], datetime]) -> LLMGateway:
    prompts = _load_prompts(config)
    providers = {
        name: _build(LLMS, "大模型", name, provider, config.root)
        for name, provider in config.llm.providers.items()
        if provider.enabled
    }
    routes: dict[str, TaskRoute] = {}
    for task, setting in config.llm.tasks.items():
        if setting.provider not in providers:
            raise ConfigError(f"LLM 任务 {task} 引用了未启用的提供方：{setting.provider}")
        routes[task] = TaskRoute(
            providers[setting.provider], setting.model, prompts[setting.prompt]
        )
    return LLMGateway(routes, store.runs, config.llm.budget.daily_usd, clock)


def _build_channels(config: LoadedConfig) -> dict[str, ChannelBinding]:
    channels: dict[str, ChannelBinding] = {}
    for name, channel in config.channels.items():
        if channel.enabled:
            channels[name] = ChannelBinding(
                name=name,
                visibility=channel.visibility,
                renderer=_pick(RENDERERS, "渲染", channel.renderer)(),
                publisher=_build(PUBLISHERS, "发布", name, channel, config.root),
            )
    return channels


def build_context(config: LoadedConfig, clock: Callable[[], datetime] | None = None) -> AppContext:
    now = clock or (lambda: datetime.now(UTC))
    store = SqlStore(config.database_url)
    chunker = config.app.chunker
    return AppContext(
        store=store,
        sources={
            name: _build(SOURCES, "信号源", name, source, config.root)
            for name, source in config.sources.items()
            if source.enabled
        },
        fulltext=tuple(
            _build(FULLTEXT, "全文", item.adapter, item, config.root)
            for item in config.app.fulltext
            if item.enabled
        ),
        parser=_build(PARSERS, "解析", config.app.parser.adapter, config.app.parser, config.root),
        llm=_build_llm(config, store, now),
        channels=_build_channels(config),
        blobs=LocalBlobStore(config.path(config.app.blob_root)),
        archive=LocalBlobStore(config.path(config.app.archive_dir)),
        clock=now,
        settings=RuntimeSettings(
            max_attempts=config.app.jobs.max_attempts,
            backoff_seconds=config.app.jobs.backoff_seconds,
            max_defer_hours=config.app.jobs.max_defer_hours,
            assess_token_budget=config.app.assess.token_budget,
            chunker=ChunkerConfig(
                chunker.target_tokens, chunker.max_tokens, chunker.overlap_sentences
            ),
            timezone=config.timezone,
        ),
        topics=config.topics,
        journal_sets=config.journal_sets,
        rubrics=config.rubrics,
        routes=config.routes,
    )
