"""运行时依赖。所有阶段都通过它拿到接口，不直接构造任何实现。"""

from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from datetime import datetime
from zoneinfo import ZoneInfo

from litgleaner.application.llm_gateway import LLMGateway
from litgleaner.domain.chunker import ChunkerConfig
from litgleaner.domain.embedding import EmbeddingSettings
from litgleaner.domain.glossary import Glossary
from litgleaner.domain.language import LanguageCode
from litgleaner.domain.memory_policy import MemoryPolicy
from litgleaner.domain.outline import TopicOutlineSettings
from litgleaner.domain.relate_settings import RelateSettings
from litgleaner.domain.routing import Route
from litgleaner.domain.rubric import Rubric
from litgleaner.domain.survey import SurveySettings
from litgleaner.domain.topic import Topic
from litgleaner.domain.topic_map import TopicMapSettings
from litgleaner.ports.blobstore import BlobStore
from litgleaner.ports.embedder import Embedder
from litgleaner.ports.fulltext import FullTextProvider
from litgleaner.ports.library import ReferenceLibrary
from litgleaner.ports.parser import Parser
from litgleaner.ports.publisher import Publisher, Renderer
from litgleaner.ports.source import Source
from litgleaner.ports.source_capture import SourceCapture
from litgleaner.ports.store import Store


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
    ui_language: LanguageCode = "zh-CN"
    reading_language: LanguageCode = "zh-CN"
    survey: SurveySettings = field(default_factory=SurveySettings)
    survey_sources_version: str = ""
    map: TopicMapSettings = field(default_factory=TopicMapSettings)
    outline: TopicOutlineSettings = field(default_factory=TopicOutlineSettings)
    embedding: EmbeddingSettings = field(default_factory=EmbeddingSettings)
    memory_policy: MemoryPolicy = field(default_factory=MemoryPolicy)
    relate: RelateSettings = field(default_factory=RelateSettings)


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
    glossaries: Mapping[str, Glossary]
    journal_sets: Mapping[str, tuple[str, ...]]
    rubrics: Mapping[str, Rubric]
    routes: tuple[Route, ...]
    library: ReferenceLibrary | None = None
    embedder: Embedder | None = None
    source_capture: SourceCapture | None = None

    def now_iso(self) -> str:
        return self.clock().isoformat()
