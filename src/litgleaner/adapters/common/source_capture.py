"""生产来源采集凭据：每份输入先可靠保存，再交回适配器解析。"""

import hashlib
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import datetime
from typing import Any

import zstandard
from ulid import ULID

from litgleaner.adapters.common.capture import capture_inputs
from litgleaner.domain.query import Query
from litgleaner.ports.blobstore import BlobStore
from litgleaner.ports.source import FetchResult, Source
from litgleaner.ports.source_capture import (
    CaptureAttempt,
    CapturedInput,
    CaptureRepo,
    CaptureScope,
    SourceRecipe,
)


@dataclass
class _InputSink:
    attempt_id: str
    repo: CaptureRepo
    blobs: BlobStore
    position: int = 0

    def append(self, kind: str, content: bytes) -> None:
        digest = hashlib.sha256(content).hexdigest()
        key = f"source_inputs/{self.attempt_id}/{self.position:06d}-{digest}.zst"
        self.blobs.put(key, zstandard.ZstdCompressor().compress(content))
        self.repo.append(
            CapturedInput(self.attempt_id, self.position, kind, key, digest, len(content))
        )
        self.position += 1


def _result_boundary(result: FetchResult) -> Mapping[str, Any]:
    return {
        "records": len(result.records),
        "references": len(result.references),
        "citations": len(result.citations),
        "next_cursor": result.next_cursor,
        "truncated": result.truncated,
    }


class DurableSourceCapture:
    def __init__(
        self,
        repo: CaptureRepo,
        blobs: BlobStore,
        clock: Callable[[], datetime],
        recipes: Mapping[str, SourceRecipe],
        sources: Mapping[str, Source],
    ) -> None:
        self._repo, self._blobs, self._clock = repo, blobs, clock
        self._recipes = dict(recipes)
        self._sources = dict(sources)

    def fetch(
        self, source: Source, query: Query, cursor: str | None, scope: CaptureScope
    ) -> FetchResult:
        attempt = CaptureAttempt(
            id=str(ULID()),
            source=source.name,
            query=query.model_dump(mode="json"),
            query_hash=query.hash,
            cursor=cursor,
            scope=scope,
            recipe=self._recipes.get(source.name)
            if self._sources.get(source.name) is source
            else None,
            started_at=self._clock().isoformat(),
        )
        self._repo.start(attempt)
        sink = _InputSink(attempt.id, self._repo, self._blobs)
        try:
            with capture_inputs(sink):
                result = source.fetch(query, cursor)
        except Exception as error:
            self._repo.finish(
                attempt.id, "raised", self._clock().isoformat(), error_type=type(error).__name__
            )
            raise
        self._repo.finish(
            attempt.id, "returned", self._clock().isoformat(), result=_result_boundary(result)
        )
        return result
