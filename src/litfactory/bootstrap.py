"""组装程序：配置里的适配器名称在这里变成具体实现。新增适配器只改注册表。"""

import os
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from litfactory.adapters.blobstore.local import LocalBlobStore
from litfactory.adapters.fakes.fulltext import FakeFullText
from litfactory.adapters.fakes.library import FakeLibrary
from litfactory.adapters.fakes.llm import FakeLLM
from litfactory.adapters.fakes.parser import FakeParser
from litfactory.adapters.fakes.publisher import FakePublisher
from litfactory.adapters.fakes.source import FakeSource
from litfactory.adapters.feishu.api import APP_ID_ENV, APP_SECRET_ENV, FeishuMessenger
from litfactory.adapters.feishu.card import FeishuCardRenderer
from litfactory.adapters.feishu.listener import FeishuListener, answerers_from
from litfactory.adapters.feishu.publisher import FeishuPublisher
from litfactory.adapters.fulltext.arxiv import ArxivFullText
from litfactory.adapters.fulltext.core import CoreFullText
from litfactory.adapters.fulltext.crossref_tdm import CrossrefTdmFullText
from litfactory.adapters.fulltext.europe_pmc import EuropePmcFullText
from litfactory.adapters.fulltext.openalex import OpenAlexFullText
from litfactory.adapters.fulltext.semantic_scholar import SemanticScholarFullText
from litfactory.adapters.fulltext.unpaywall import UnpaywallFullText
from litfactory.adapters.llm.anthropic import AnthropicMessagesLLM
from litfactory.adapters.llm.base import HttpLLM
from litfactory.adapters.llm.gemini import GeminiLLM
from litfactory.adapters.llm.openai import OpenAIChatLLM, OpenAIResponsesLLM
from litfactory.adapters.parsers.mineru_cloud import MinerUCloudParser
from litfactory.adapters.parsers.pdf_text import PdfTextParser
from litfactory.adapters.renderers.plain_text import PlainTextRenderer
from litfactory.adapters.sources.arxiv import ArxivSource
from litfactory.adapters.sources.core import CoreSource
from litfactory.adapters.sources.crossref import CrossrefSource
from litfactory.adapters.sources.europe_pmc import EuropePmcSource
from litfactory.adapters.sources.openalex import OpenAlexSource
from litfactory.adapters.sources.openreview import OpenReviewSource
from litfactory.adapters.sources.pubmed import PubMedSource
from litfactory.adapters.sources.semantic_scholar import SemanticScholarSource
from litfactory.adapters.zotero.fulltext import ZoteroFullText
from litfactory.adapters.zotero.local_api import (
    default_key_file,
    read_local_key,
    save_local_key,
)
from litfactory.adapters.zotero.source import ZoteroSource
from litfactory.adapters.zotero.writer_local import ZoteroLocalLibrary
from litfactory.application.context import AppContext, ChannelBinding, RuntimeSettings
from litfactory.application.llm_gateway import LLMGateway, Prompt, TaskRoute
from litfactory.domain.chunker import ChunkerConfig
from litfactory.domain.errors import ConfigError
from litfactory.infra.config import AdapterConfig, LoadedConfig
from litfactory.infra.db.store import SqlStore
from litfactory.ports.fulltext import FullTextProvider
from litfactory.ports.inbound import CardClick
from litfactory.ports.library import ReferenceLibrary
from litfactory.ports.llm import LLM
from litfactory.ports.parser import Parser
from litfactory.ports.publisher import Publisher, Renderer
from litfactory.ports.source import Source

CONTACT_EMAIL = "LITFACTORY_CONTACT_EMAIL"
S2_KEY = "SEMANTIC_SCHOLAR_API_KEY"
CORE_KEY = "CORE_API_KEY"
ZOTERO_KEY = "ZOTERO_API_KEY"
OPENALEX_KEY = "OPENALEX_API_KEY"
NCBI_KEY = "NCBI_API_KEY"


@dataclass(frozen=True)
class BuildArgs:
    name: str
    options: Mapping[str, Any]
    root: Path
    credentials: Mapping[str, str]

    optional: frozenset[str] = frozenset()

    def secret(self, env_name: str) -> str:
        """密钥只从环境变量读取，配置文件里只写变量名。"""
        if env_name not in self.credentials:
            raise ConfigError(f"{self.name} 需要在 credentials_env 中声明 {env_name}")
        return os.environ.get(env_name, "")

    def optional_secret(self, env_name: str) -> str | None:
        """可选的 key 或联系邮箱：没有也能运行，只是额度更低。必须先在配置里声明。"""
        if env_name not in self.credentials and env_name not in self.optional:
            raise ConfigError(f"{self.name} 需要在 optional_env 中声明 {env_name}")
        return os.environ.get(env_name) or None

    def options_with_paths(self, *keys: str) -> dict[str, Any]:
        """把配置里的相对路径换成项目根目录下的绝对路径。"""
        resolved = dict(self.options)
        for key in keys:
            if key in resolved:
                resolved[key] = str(self.root / str(resolved[key]))
        return resolved

    @property
    def contact(self) -> str | None:
        """礼貌通道用的联系邮箱，所有来源共用，不是密钥，不需要逐个声明。"""
        return os.environ.get(CONTACT_EMAIL) or None

    def path(self, option: str) -> Path:
        return self.root / str(self.options[option])

    def optional_path(self, option: str) -> Path | None:
        return self.path(option) if option in self.options else None


SOURCES: Mapping[str, Callable[[BuildArgs], Source]] = {
    "fake_source": lambda a: FakeSource(a.name, a.path("records_file")),
    "crossref": lambda a: CrossrefSource(a.name, a.options, contact_email=a.contact),
    "openalex": lambda a: OpenAlexSource(
        a.name, a.options, contact_email=a.contact, api_key=a.optional_secret(OPENALEX_KEY)
    ),
    "arxiv": lambda a: ArxivSource(a.name, a.options, contact_email=a.contact),
    "openreview": lambda a: OpenReviewSource(a.name, a.options, contact_email=a.contact),
    "pubmed": lambda a: PubMedSource(
        a.name, a.options, contact_email=a.contact, api_key=a.optional_secret(NCBI_KEY)
    ),
    "europe_pmc": lambda a: EuropePmcSource(a.name, a.options, contact_email=a.contact),
    "semantic_scholar": lambda a: SemanticScholarSource(
        a.name, a.options, contact_email=a.contact, api_key=a.optional_secret(S2_KEY)
    ),
    "core": lambda a: CoreSource(
        a.name, a.options, contact_email=a.contact, api_key=a.secret(CORE_KEY)
    ),
    "zotero": lambda a: ZoteroSource(
        a.name, a.options, contact_email=a.contact, api_key=a.optional_secret(ZOTERO_KEY)
    ),
}
FULLTEXT: Mapping[str, Callable[[BuildArgs], FullTextProvider]] = {
    "fake_fulltext": lambda _: FakeFullText(),
    "crossref_tdm": lambda a: CrossrefTdmFullText(a.options, contact_email=a.contact),
    "arxiv": lambda a: ArxivFullText(a.options, contact_email=a.contact),
    "europe_pmc": lambda a: EuropePmcFullText(a.options, contact_email=a.contact),
    "unpaywall": lambda a: UnpaywallFullText(a.options, contact_email=a.contact),
    "openalex": lambda a: OpenAlexFullText(
        a.options, contact_email=a.contact, api_key=a.optional_secret(OPENALEX_KEY)
    ),
    "semantic_scholar": lambda a: SemanticScholarFullText(
        a.options, contact_email=a.contact, api_key=a.optional_secret(S2_KEY)
    ),
    "core": lambda a: CoreFullText(a.options, contact_email=a.contact, api_key=a.secret(CORE_KEY)),
    "zotero": lambda a: ZoteroFullText(
        a.options, contact_email=a.contact, api_key=a.optional_secret(ZOTERO_KEY)
    ),
}
PARSERS: Mapping[str, Callable[[BuildArgs], Parser]] = {
    "fake_parser": lambda _: FakeParser(),
    "pdf_text": lambda _: PdfTextParser(),
    "mineru_cloud": lambda a: MinerUCloudParser(
        a.secret("MINERU_API_TOKEN"), a.options_with_paths("state_dir")
    ),
}


def _llm_key(args: BuildArgs) -> str | None:
    """credentials_env 写 0 个（本地服务）或 1 个变量名。声明了就必须有值，启动时报错。"""
    if len(args.credentials) > 1:
        raise ConfigError(f"大模型提供方 {args.name} 的 credentials_env 只能写一个变量")
    env = next(iter(args.credentials), None)
    if env is None:
        return None
    key = args.secret(env)
    if not key:
        raise ConfigError(f"大模型提供方 {args.name} 需要环境变量 {env}")
    return key


LLMS: Mapping[str, Callable[[BuildArgs], LLM]] = {
    "fake_llm": lambda _: FakeLLM(),
    "anthropic_messages": lambda a: AnthropicMessagesLLM(a.name, a.options, _llm_key(a)),
    "openai_chat": lambda a: OpenAIChatLLM(a.name, a.options, _llm_key(a)),
    "openai_responses": lambda a: OpenAIResponsesLLM(a.name, a.options, _llm_key(a)),
    "gemini": lambda a: GeminiLLM(a.name, a.options, _llm_key(a)),
}
PUBLISHERS: Mapping[str, Callable[[BuildArgs], Publisher]] = {
    "fake_publisher": lambda a: FakePublisher(a.optional_path("output_file")),
    "feishu": lambda a: FeishuPublisher(
        a.options, FeishuMessenger(a.secret(APP_ID_ENV), a.secret(APP_SECRET_ENV))
    ),
}
LIBRARIES: Mapping[str, Callable[[BuildArgs], ReferenceLibrary]] = {
    # 本地 key 由 litfactory zotero authorize 写入私有文件，也可以用环境变量覆盖。
    "fake_library": lambda _: FakeLibrary(),
    "zotero_local": lambda a: ZoteroLocalLibrary(
        a.options, api_key=read_local_key(os.environ, default_key_file())
    ),
}
RENDERERS: Mapping[str, Callable[[], Renderer]] = {
    "plain_text": PlainTextRenderer,
    "feishu_card": FeishuCardRenderer,
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
    optional = frozenset(config.optional_env)
    return factory(BuildArgs(name, config.options, root, credentials, optional))


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
        llm = providers[setting.provider]
        if isinstance(llm, HttpLLM):
            llm.check_model(setting.model)
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


def build_library(config: LoadedConfig) -> ReferenceLibrary | None:
    library = config.app.library
    if library is None or not library.enabled:
        return None
    return _build(LIBRARIES, "文献库", library.adapter, library, config.root)


def _zotero_local(config: LoadedConfig) -> ZoteroLocalLibrary:
    library = config.app.library
    if library is None or library.adapter != "zotero_local":
        raise ConfigError("config/app.yaml 的 library 不是 zotero_local，见 docs/CHANNELS.md")
    return ZoteroLocalLibrary(
        library.options, api_key=read_local_key(os.environ, default_key_file())
    )


def authorize_zotero(config: LoadedConfig) -> Path:
    """弹出 Zotero 授权窗口，把 key 存进只有本人可读的文件。返回文件路径，不返回 key。"""
    writer = _zotero_local(config)
    try:
        key = writer.authorize()
    finally:
        writer.close()
    path = default_key_file()
    save_local_key(key, path)
    return path


def zotero_status(config: LoadedConfig) -> list[str]:
    """只读检查：Zotero 在运行、本地 API 已开启、有 key、目标分组。不写入任何东西。"""
    writer = _zotero_local(config)
    try:
        writer.probe()
        lines = ["Zotero 正在运行，本地 API 已开启"]
        key = read_local_key(os.environ, default_key_file())
        lines.append("已授权" if key else "未授权：运行 litfactory zotero authorize")
        lines.append(f"目标分组：{writer.collection_name}（第一次写入时自动创建）")
        return lines
    finally:
        writer.close()


def build_feishu_listener(
    config: LoadedConfig, channel: str, on_click: Callable[[CardClick], None]
) -> tuple[FeishuListener, Callable[[str, str], None]]:
    """返回监听器和“更新某条消息的卡片”的函数。凭据与这个渠道的推送共用。"""
    settings = config.channels.get(channel)
    if settings is None or settings.adapter != "feishu" or not settings.enabled:
        raise ConfigError(f"config/channels.yaml 里没有启用的飞书渠道 {channel}")
    args = BuildArgs(
        channel, settings.options, config.root, {env: env for env in settings.credentials_env}
    )
    app_id, app_secret = args.secret(APP_ID_ENV), args.secret(APP_SECRET_ENV)
    messenger = FeishuMessenger(app_id, app_secret)
    listener = FeishuListener(app_id, app_secret, answerers_from(settings.options), on_click)
    return listener, messenger.update


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
        library=build_library(config),
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
