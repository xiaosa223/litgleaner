"""组装程序：配置里的适配器名称在这里变成具体实现。新增适配器只改注册表。"""

import os
from collections.abc import Callable, Mapping
from datetime import UTC, datetime
from pathlib import Path

from litgleaner import bootstrap_args
from litgleaner.adapters.blobstore.local import LocalBlobStore
from litgleaner.adapters.embeddings.fastembed import LocalEmbedder
from litgleaner.adapters.fakes.fulltext import FakeFullText
from litgleaner.adapters.fakes.library import FakeLibrary
from litgleaner.adapters.fakes.llm import FakeLLM
from litgleaner.adapters.fakes.parser import FakeParser
from litgleaner.adapters.fakes.publisher import FakePublisher
from litgleaner.adapters.fakes.source import FakeSource
from litgleaner.adapters.feishu.api import APP_ID_ENV, APP_SECRET_ENV, FeishuMessenger
from litgleaner.adapters.feishu.card import FeishuCardRenderer
from litgleaner.adapters.feishu.listener import FeishuListener, answerers_from
from litgleaner.adapters.feishu.publisher import FeishuPublisher
from litgleaner.adapters.fulltext.arxiv import ArxivFullText
from litgleaner.adapters.fulltext.core import CoreFullText
from litgleaner.adapters.fulltext.crossref_tdm import CrossrefTdmFullText
from litgleaner.adapters.fulltext.europe_pmc import EuropePmcFullText
from litgleaner.adapters.fulltext.openalex import OpenAlexFullText
from litgleaner.adapters.fulltext.semantic_scholar import SemanticScholarFullText
from litgleaner.adapters.fulltext.unpaywall import UnpaywallFullText
from litgleaner.adapters.llm.anthropic import AnthropicMessagesLLM
from litgleaner.adapters.llm.base import HttpLLM
from litgleaner.adapters.llm.gemini import GeminiLLM
from litgleaner.adapters.llm.openai import OpenAIChatLLM, OpenAIResponsesLLM
from litgleaner.adapters.parsers.mineru_cloud import MinerUCloudParser
from litgleaner.adapters.parsers.pdf_text import PdfTextParser
from litgleaner.adapters.renderers.plain_text import PlainTextRenderer
from litgleaner.adapters.secrets import SystemSecretStore
from litgleaner.adapters.sources.arxiv import ArxivSource
from litgleaner.adapters.sources.core import CoreSource
from litgleaner.adapters.sources.crossref import CrossrefSource
from litgleaner.adapters.sources.europe_pmc import EuropePmcSource
from litgleaner.adapters.sources.openalex import OpenAlexSource
from litgleaner.adapters.sources.openreview import OpenReviewSource
from litgleaner.adapters.sources.pubmed import PubMedSource
from litgleaner.adapters.sources.semantic_scholar import SemanticScholarSource
from litgleaner.adapters.zotero.fulltext import ZoteroFullText
from litgleaner.adapters.zotero.local_api import default_key_file, read_local_key, save_local_key
from litgleaner.adapters.zotero.source import ZoteroSource
from litgleaner.adapters.zotero.writer_local import ZoteroLocalLibrary
from litgleaner.application.context import AppContext, ChannelBinding, RuntimeSettings
from litgleaner.application.llm_gateway import LLMGateway, Prompt, TaskRoute
from litgleaner.bootstrap_args import BuildArgs, _llm_key
from litgleaner.domain.chunker import ChunkerConfig
from litgleaner.domain.embedding import EmbeddingSettings
from litgleaner.domain.errors import ConfigError
from litgleaner.domain.hashing import stable_hash
from litgleaner.infra.config import AdapterConfig, EmbeddingConfig, LoadedConfig
from litgleaner.infra.db.store import SqlStore
from litgleaner.ports.embedder import Embedder
from litgleaner.ports.fulltext import FullTextProvider
from litgleaner.ports.inbound import CardClick
from litgleaner.ports.library import ReferenceLibrary
from litgleaner.ports.llm import LLM
from litgleaner.ports.parser import Parser
from litgleaner.ports.publisher import Publisher, Renderer
from litgleaner.ports.secrets import SecretStore
from litgleaner.ports.source import Source
from litgleaner.ports.store import Store

CONTACT_EMAIL = bootstrap_args.CONTACT_EMAIL
S2_KEY = "SEMANTIC_SCHOLAR_API_KEY"
CORE_KEY = "CORE_API_KEY"
ZOTERO_KEY = "ZOTERO_API_KEY"
OPENALEX_KEY = "OPENALEX_API_KEY"
NCBI_KEY = "NCBI_API_KEY"


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
    # 本地 key 由 litgleaner zotero authorize 写入私有文件，也可以用环境变量覆盖。
    "fake_library": lambda _: FakeLibrary(),
    "zotero_local": lambda a: ZoteroLocalLibrary(
        a.options, api_key=read_local_key(os.environ, default_key_file())
    ),
}
RENDERERS: Mapping[str, Callable[[], Renderer]] = {
    "plain_text": PlainTextRenderer,
    "feishu_card": FeishuCardRenderer,
}
EMBEDDERS: Mapping[str, Callable[[EmbeddingConfig, Path], Embedder]] = {
    "fastembed": lambda item, path: LocalEmbedder(
        item.model, item.dimensions, path, threads=item.threads, batch_size=item.batch_size
    ),
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
    *,
    secrets: SecretStore | None = None,
) -> T:
    factory = _pick(registry, kind, config.adapter)
    credentials = {env: env for env in config.credentials_env}
    optional = frozenset(config.optional_env)
    return factory(BuildArgs(name, config.options, root, credentials, optional, secrets))


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


def build_model_client(config: LoadedConfig, name: str, secrets: SecretStore | None = None) -> LLM:
    return _build(LLMS, "大模型", name, config.llm.providers[name], config.root, secrets=secrets)


def build_llm(
    config: LoadedConfig,
    store: Store,
    clock: Callable[[], datetime],
    secrets: SecretStore | None = None,
) -> LLMGateway:
    prompts = _load_prompts(config)
    providers = {
        name: build_model_client(config, name, secrets)
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
            providers[setting.provider],
            setting.model,
            prompts[setting.prompt],
            provider_version=stable_hash(
                {
                    "name": setting.provider,
                    "adapter": config.llm.providers[setting.provider].adapter,
                    "options": dict(config.llm.providers[setting.provider].options),
                }
            ),
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


def build_embedder(config: LoadedConfig) -> Embedder | None:
    item = config.memory.embedding
    if not item.enabled:
        return None
    return _pick(EMBEDDERS, "本地嵌入", item.adapter)(item, config.embedding_cache_dir)


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
        lines.append("已授权" if key else "未授权：运行 litgleaner zotero authorize")
        lines.append(f"目标分组：{writer.collection_name}（第一次写入时自动创建）")
        return lines
    finally:
        writer.close()


def build_feishu_listener(
    config: LoadedConfig, channel: str | None, on_click: Callable[[CardClick], None]
) -> tuple[FeishuListener, Callable[[str, str], None]]:
    """组装指定渠道或同一应用的全部渠道；无可用渠道时不读凭据、不加载 SDK。"""
    channels = {
        name: item
        for name, item in config.channels.items()
        if item.enabled and item.adapter == "feishu" and channel in (None, name)
    }
    if not channels:
        raise ConfigError("inbound_disabled")
    name, settings = next(iter(channels.items()))
    envs = set.intersection(*(set(item.credentials_env) for item in channels.values()))
    args = BuildArgs(
        name, settings.options, config.root, {env: env for env in envs}, secrets=SystemSecretStore()
    )
    app_id, app_secret = args.secret(APP_ID_ENV), args.secret(APP_SECRET_ENV)
    allowed = {name: answerers_from(item.options) for name, item in channels.items()}
    listener = FeishuListener(
        app_id, app_secret, frozenset().union(*allowed.values()), on_click, channels=allowed
    )
    return listener, FeishuMessenger(app_id, app_secret).update


def _survey_sources_version(config: LoadedConfig) -> str:
    sources = {name: item.model_dump(mode="json") for name, item in config.sources.items()}
    fulltext = [item.model_dump(mode="json") for item in config.app.fulltext]
    return stable_hash((sources, fulltext, config.app.parser.model_dump(mode="json")))


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
        llm=build_llm(config, store, now),
        channels=_build_channels(config),
        library=build_library(config),
        embedder=build_embedder(config),
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
            ui_language=config.app.language.ui,
            reading_language=config.app.language.reading,
            survey=config.app.survey,
            survey_sources_version=_survey_sources_version(config),
            map=config.app.map,
            outline=config.app.outline,
            embedding=EmbeddingSettings.model_validate(
                config.memory.embedding.model_dump(include={"max_input_chars", "batch_size"})
            ),
            memory_policy=config.memory.candidates,
        ),
        topics=config.topics,
        glossaries=config.glossaries,
        journal_sets=config.journal_sets,
        rubrics=config.rubrics,
        routes=config.routes,
    )
