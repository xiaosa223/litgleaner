"""运行时依赖。所有阶段都通过它拿到接口，不直接构造任何实现。"""

from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import datetime
from zoneinfo import ZoneInfo

from litfactory.application.llm_gateway import LLMGateway
from litfactory.domain.chunker import ChunkerConfig
from litfactory.domain.routing import Route
from litfactory.domain.rubric import Rubric
from litfactory.domain.topic import Topic
from litfactory.ports.blobstore import BlobStore
from litfactory.ports.fulltext import FullTextProvider
from litfactory.ports.parser import Parser
from litfactory.ports.publisher import Publisher, Renderer
from litfactory.ports.source import Source
from litfactory.ports.store import Store


@dataclass(frozen=True)
class ChannelBinding:
    name: str
    visibility: str
    renderer: Renderer
    publisher: Publisher


@dataclass(frozen=True)
class RuntimeSettings:
    max_attempts: int
    backoff_seconds: int
    max_defer_hours: int
    assess_token_budget: int
    chunker: ChunkerConfig
    timezone: ZoneInfo


@dataclass(frozen=True)
class AppContext:
    store: Store
    sources: Mapping[str, Source]
    fulltext: tuple[FullTextProvider, ...]
    parser: Parser
    llm: LLMGateway
    channels: Mapping[str, ChannelBinding]
    blobs: BlobStore
    archive: BlobStore
    clock: Callable[[], datetime]
    settings: RuntimeSettings
    topics: Mapping[str, Topic]
    journal_sets: Mapping[str, tuple[str, ...]]
    rubrics: Mapping[str, Rubric]
    routes: tuple[Route, ...]

    def now_iso(self) -> str:
        return self.clock().isoformat()
