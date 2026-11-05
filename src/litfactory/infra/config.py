"""配置加载和校验。错误在启动时一次报清楚，并指出是哪个文件。"""

import json
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

import yaml
from pydantic import Field, ValidationError

from litfactory.domain.base import FrozenModel
from litfactory.domain.errors import ConfigError
from litfactory.domain.routing import ALL_TOPICS, PUBLIC_TOPICS, Route
from litfactory.domain.rubric import Rubric
from litfactory.domain.topic import Topic


class AdapterConfig(FrozenModel):
    adapter: str
    enabled: bool = True
    credentials_env: tuple[str, ...] = ()
    optional_env: tuple[str, ...] = ()
    options: Mapping[str, Any] = Field(default_factory=dict)


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


class AppSettings(FrozenModel):
    database: str
    blob_root: str
    archive_dir: str
    prompts_dir: str
    timezone: str
    jobs: JobSettings = JobSettings()
    chunker: ChunkerSettings = ChunkerSettings()
    assess: AssessSettings = AssessSettings()
    fulltext: tuple[AdapterConfig, ...] = ()
    parser: AdapterConfig


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
    providers: Mapping[str, AdapterConfig]
    tasks: Mapping[Literal["triage", "extract", "assess"], LLMTask]
    budget: LLMBudget


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

    @property
    def database_url(self) -> str:
        return f"sqlite:///{self.root / self.app.database}"

    def path(self, relative: str) -> Path:
        return self.root / relative


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


def _load_topics(directory: Path) -> dict[str, Topic]:
    topics: dict[str, Topic] = {}
    for path in sorted(directory.glob("*.yaml")):
        topic = _load(path, Topic)
        if topic.id != path.stem:
            raise ConfigError(f"主题文件 {path} 的 id 必须和文件名一致，当前为 {topic.id}")
        topics[topic.id] = topic
    return topics


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
        llm=_load(directory / "llm.yaml", LLMFile),
        topics=topics,
        rubrics=rubrics,
        journal_sets=journals,
        timezone=_zone(app.timezone),
    )
