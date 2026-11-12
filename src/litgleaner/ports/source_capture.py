"""来源原始输入留档：字节与事实分离，凭据可在解析失败后列举。"""

from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any, Literal, Protocol

from litgleaner.domain.query import Query
from litgleaner.ports.source import FetchResult, Source

CapturePurpose = Literal["discover", "inbox", "references", "survey"]
CaptureStatus = Literal["started", "returned", "raised"]


@dataclass(frozen=True)
class SourceRecipe:
    adapter: str
    options: Mapping[str, Any]
    option_fields: tuple[str, ...]
    credentials: Mapping[str, bool]
    contact: str | None


@dataclass(frozen=True)
class CaptureScope:
    purpose: CapturePurpose
    owner: Mapping[str, Any]


@dataclass(frozen=True)
class CaptureAttempt:
    id: str
    source: str
    query: Mapping[str, Any]
    query_hash: str
    cursor: str | None
    scope: CaptureScope
    recipe: SourceRecipe | None
    started_at: str
    status: CaptureStatus = "started"
    finished_at: str | None = None
    error_type: str | None = None
    result: Mapping[str, Any] | None = None


@dataclass(frozen=True)
class CapturedInput:
    attempt_id: str
    position: int
    kind: str
    blob_key: str
    sha256: str
    size: int


class CaptureRepo(Protocol):
    def start(self, attempt: CaptureAttempt) -> None: ...
    def append(self, item: CapturedInput) -> None: ...
    def finish(
        self,
        attempt_id: str,
        status: CaptureStatus,
        finished_at: str,
        *,
        result: Mapping[str, Any] | None = None,
        error_type: str | None = None,
    ) -> None: ...
    def get(self, attempt_id: str) -> CaptureAttempt | None: ...
    def inputs(self, attempt_id: str) -> tuple[CapturedInput, ...]: ...
    def list(
        self, source: str, since: str | None = None, *, limit: int = 100, after: str | None = None
    ) -> tuple[CaptureAttempt, ...]: ...


class SourceCapture(Protocol):
    def fetch(
        self, source: Source, query: Query, cursor: str | None, scope: CaptureScope
    ) -> FetchResult: ...


class CaptureSink(Protocol):
    def append(self, kind: str, content: bytes) -> None: ...
