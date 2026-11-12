"""配置加载和校验。错误在启动时一次报清楚，并指出是哪个文件。"""

import json
import locale
from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Literal
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

import yaml
from pydantic import Field, ValidationError, field_validator, model_validator

from litgleaner.domain.base import FrozenModel
from litgleaner.domain.embedding import EmbeddingSettings
from litgleaner.domain.errors import ConfigError
from litgleaner.domain.glossary import Glossary
from litgleaner.domain.language import LanguageCode
from litgleaner.domain.outline import TopicOutlineSettings
from litgleaner.domain.routing import ALL_TOPICS, PUBLIC_TOPICS, Route
from litgleaner.domain.rubric import Rubric
from litgleaner.domain.survey import SurveySettings
from litgleaner.domain.topic import Topic
from litgleaner.domain.topic_map import TopicMapSettings


class AdapterConfig(FrozenModel):
    adapter: str
    enabled: bool = True
    credentials_env: tuple[str, ...] = ()
    optional_env: tuple[str, ...] = ()
    options: Mapping[str, Any] = Field(default_factory=dict)


class EmbeddingConfig(EmbeddingSettings):
    max_input_chars: int = Field(default=8000, ge=100, le=100000, strict=True)
    batch_size: int = Field(default=16, ge=1, le=256, strict=True)
    adapter: str = Field(default="fastembed", min_length=1, max_length=100)
    enabled: bool = Field(default=True, strict=True)
    model: str = Field(
        default="sentence-transformers/paraphrase-multilingual-MiniLM-L12-v2",
        min_length=1,
        max_length=300,
    )
    dimensions: int = Field(default=384, ge=1, le=65536, strict=True)
    cache_dir: str | None = Field(default=None, min_length=1)
    threads: int = Field(default=1, ge=1, le=64, strict=True)

    @field_validator("adapter", "model", "cache_dir")
    @classmethod
    def _nonblank(cls, value: str | None) -> str | None:
        if value is not None and not value.strip():
            raise ValueError("不能只包含空白")
        return value.strip() if value is not None else value


class MemoryConfig(FrozenModel):
    embedding: EmbeddingConfig = Field(default_factory=EmbeddingConfig)


class ChannelConfig(AdapterConfig):
    renderer: str
    visibility: Literal["private", "public"]


class JobSettings(FrozenModel):
    max_attempts: int = Field(default=3, ge=1)
    backoff_seconds: int = Field(default=300, ge=0)
    max_defer_hours: int = Field(default=48, gt=0)


class ChunkerSettings(FrozenModel):
    target_tokens: int = Field(default=800, gt=0)
    max_tokens: int = Field(default=1200, gt=0)
    overlap_sentences: int = Field(default=1, ge=0)


class AssessSettings(FrozenModel):
    token_budget: int = Field(default=6000, gt=0)


class BackupSettings(FrozenModel):
    dir: str | None = None  # 不写时放在数据库所在目录的 backups/ 下
    keep: int = Field(default=14, ge=1)


def _system_language() -> LanguageCode:
    try:
        name = locale.getlocale()[0]
    except (ValueError, TypeError, locale.Error):
        return "zh-CN"  # 系统 locale 损坏时按首发语言回退，不阻止启动
    return "zh-CN" if (name or "").lower().startswith("zh") else "en"


class LanguageSettings(FrozenModel):
    ui: LanguageCode = Field(default_factory=_system_language)
    reading: LanguageCode = "zh-CN"

    @model_validator(mode="before")
    @classmethod
    def _follow_ui(cls, value: object) -> object:
        if not isinstance(value, Mapping):
            return value
        settings = dict(value)
        if "ui" not in settings:
            settings["ui"] = _system_language()
        if "reading" not in settings:
            settings["reading"] = settings["ui"]
        return settings


class AppSettings(FrozenModel):
    database: str
    blob_root: str
    archive_dir: str
    prompts_dir: str
    timezone: str
    language: LanguageSettings = Field(default_factory=LanguageSettings)
    jobs: JobSettings = JobSettings()
    chunker: ChunkerSettings = ChunkerSettings()
    assess: AssessSettings = AssessSettings()
    backup: BackupSettings = BackupSettings()
    fulltext: tuple[AdapterConfig, ...] = ()
    parser: AdapterConfig = AdapterConfig(adapter="pdf_text")
    library: AdapterConfig | None = None  # 加入文献库，不写或 enabled: false 时关闭
    survey: SurveySettings = Field(default_factory=SurveySettings)
    map: TopicMapSettings = Field(default_factory=TopicMapSettings)
    outline: TopicOutlineSettings = Field(default_factory=TopicOutlineSettings)


class SourcesFile(FrozenModel):
    sources: Mapping[str, AdapterConfig]


class ChannelsFile(FrozenModel):
    channels: Mapping[str, ChannelConfig]
    routes: tuple[Route, ...] = ()


class LLMTask(FrozenModel):
    provider: str
    model: str
    prompt: str


class LLMBudget(FrozenModel):
    daily_usd: float = Field(ge=0)


class LLMFile(FrozenModel):
    providers: Mapping[str, AdapterConfig] = Field(default_factory=dict)
    tasks: Mapping[
        Literal["triage", "extract", "assess", "translate", "ask", "map", "outline"], LLMTask
    ] = Field(default_factory=dict)
    budget: LLMBudget = LLMBudget(daily_usd=2)


class JournalSet(FrozenModel):
    issns: tuple[str, ...]


@dataclass(frozen=True)
class LoadedConfig:
    root: Path
    app: AppSettings
    sources: Mapping[str, AdapterConfig]
    channels: Mapping[str, ChannelConfig]
    routes: tuple[Route, ...]
    llm: LLMFile
    topics: Mapping[str, Topic]
    rubrics: Mapping[str, Rubric]
    journal_sets: Mapping[str, tuple[str, ...]]
    timezone: ZoneInfo
    glossaries: Mapping[str, Glossary] = field(default_factory=dict)
    config_dir: Path | None = None
    memory: MemoryConfig = field(default_factory=MemoryConfig)

    @property
    def settings_dir(self) -> Path:
        return self.config_dir or self.root / "config"

    @property
    def database_url(self) -> str:
        return f"sqlite:///{self.root / self.app.database}"

    def path(self, relative: str) -> Path:
        return self.root / relative

    @property
    def database_path(self) -> Path:
        return self.path(self.app.database)

    @property
    def backup_dir(self) -> Path:
        if self.app.backup.dir:
            return self.path(self.app.backup.dir)
        return self.database_path.parent / "backups"

    @property
    def embedding_cache_dir(self) -> Path:
        configured = self.memory.embedding.cache_dir
        return (
            self.path(configured) if configured else self.database_path.parent / "models/embeddings"
        )


def _read(path: Path) -> Any:
    try:
        text = path.read_text(encoding="utf-8")
    except OSError as error:
        raise ConfigError(f"无法读取配置文件 {path}：{error}") from error
    try:
        return json.loads(text) if path.suffix == ".json" else yaml.safe_load(text)
    except (json.JSONDecodeError, yaml.YAMLError) as error:
        raise ConfigError(f"配置文件 {path} 格式错误：{error}") from error


def _load[ModelT: FrozenModel](path: Path, model: type[ModelT]) -> ModelT:
    try:
        return model.model_validate(_read(path))
    except ValidationError as error:
        raise ConfigError(f"配置文件 {path} 校验失败：\n{error}") from error


# 同目录下的术语表和检索式草稿不是主题文件
_NOT_TOPICS = (".glossary.yaml", ".draft.yaml")


def _load_topics(directory: Path) -> dict[str, Topic]:
    topics: dict[str, Topic] = {}
    for path in sorted(directory.glob("*.yaml")):
        if path.name.endswith(_NOT_TOPICS):
            continue
        topic = _load(path, Topic)
        if topic.id != path.stem:
            raise ConfigError(f"主题文件 {path} 的 id 必须和文件名一致，当前为 {topic.id}")
        topics[topic.id] = topic
    return topics


def _load_glossaries(directory: Path, topics: Mapping[str, Topic]) -> dict[str, Glossary]:
    """config/topics/<topic-id>.glossary.yaml，可以没有（视为空表）。"""
    glossaries: dict[str, Glossary] = {}
    for path in sorted(directory.glob("*.glossary.yaml")):
        topic_id = path.name.removesuffix(".glossary.yaml")
        if topic_id not in topics:
            raise ConfigError(f"术语表 {path} 对应的主题 {topic_id} 不存在")
        glossaries[topic_id] = _load(path, Glossary)
    return glossaries


def _load_rubrics(directory: Path) -> dict[str, Rubric]:
    rubrics: dict[str, Rubric] = {}
    for path in sorted(directory.glob("*.json")):
        rubric = _load(path, Rubric)
        if rubric.id != path.stem:
            raise ConfigError(f"指标文件 {path} 的 id 必须和文件名一致，当前为 {rubric.id}")
        rubrics[rubric.id] = rubric
    return rubrics


def _check_topics(
    topics: Mapping[str, Topic],
    rubrics: Mapping[str, Rubric],
    journals: Mapping[str, tuple[str, ...]],
) -> None:
    for topic in topics.values():
        if topic.rubric not in rubrics:
            raise ConfigError(f"主题 {topic.id} 引用了不存在的指标：{topic.rubric}")
        for name in topic.journal_sets:
            if name not in journals:
                raise ConfigError(f"主题 {topic.id} 引用了不存在的期刊清单：{name}")


def _check_routes(
    file: ChannelsFile, topics: Mapping[str, Topic], rubrics: Mapping[str, Rubric]
) -> None:
    tiers = {tier.key for rubric in rubrics.values() for tier in rubric.tiers}
    tiers |= {rubric.provisional.tier for rubric in rubrics.values()}
    for route in file.routes:
        if route.channel not in file.channels:
            raise ConfigError(f"路由 {route.id} 引用了不存在的渠道：{route.channel}")
        if unknown := set(route.tiers) - tiers:
            raise ConfigError(f"路由 {route.id} 引用了不存在的等级：{sorted(unknown)}")
        if route.topic not in {ALL_TOPICS, PUBLIC_TOPICS, *topics}:
            raise ConfigError(f"路由 {route.id} 引用了不存在的主题：{route.topic}")
    ids = [route.id for route in file.routes]
    if len(ids) != len(set(ids)):
        raise ConfigError("路由 id 不能重复")


def _zone(name: str) -> ZoneInfo:
    try:
        return ZoneInfo(name)
    except (ZoneInfoNotFoundError, ValueError) as error:
        raise ConfigError(f"无效的时区：{name}") from error


def load_config(config_dir: Path) -> LoadedConfig:
    directory = config_dir.resolve()
    app = _load(directory / "app.yaml", AppSettings)
    channels = _load(directory / "channels.yaml", ChannelsFile)
    topics = _load_topics(directory / "topics")
    rubrics = _load_rubrics(directory / "rubrics")
    journals = {
        path.stem: _load(path, JournalSet).issns
        for path in sorted(directory.glob("journals/*.yaml"))
    }
    _check_topics(topics, rubrics, journals)
    _check_routes(channels, topics, rubrics)
    return LoadedConfig(
        root=directory.parent,
        app=app,
        sources=_load(directory / "sources.yaml", SourcesFile).sources,
        channels=channels.channels,
        routes=channels.routes,
        llm=_load(directory / "llm.yaml", LLMFile)
        if (directory / "llm.yaml").exists()
        else LLMFile(),
        topics=topics,
        rubrics=rubrics,
        journal_sets=journals,
        timezone=_zone(app.timezone),
        glossaries=_load_glossaries(directory / "topics", topics),
        config_dir=directory,
        memory=_load(directory / "memory.yaml", MemoryConfig)
        if (directory / "memory.yaml").exists()
        else MemoryConfig(),
    )
