"""真实数据库留档测试的离线来源和完整性读取。"""

import hashlib
import json

from litgleaner.adapters.common.capture import read_source_bytes
from litgleaner.application.codec import decompress
from litgleaner.domain.query import HistoricalSearch, IdentifierLookup, ReferenceList
from litgleaner.ports.source import FetchResult


class CapturingSource:
    capabilities = frozenset({IdentifierLookup, ReferenceList, HistoricalSearch})

    def __init__(self, name, result=None, error=None):
        self.name, self.result, self.error = name, result or FetchResult((), None), error
        self.calls = []

    def fetch(self, query, cursor):
        self.calls.append((query, cursor))
        read_source_bytes(lambda: json.dumps({"source": self.name}).encode())
        if self.error:
            raise self.error
        return self.result


def inputs(ctx, attempt):
    saved = ctx.store.source_captures.inputs(attempt.id)
    content = []
    for index, item in enumerate(saved):
        raw = decompress(ctx.blobs.get(item.blob_key))
        assert item.position == index and item.size == len(raw)
        assert item.sha256 == hashlib.sha256(raw).hexdigest()
        content.append((item.kind, raw))
    return content
