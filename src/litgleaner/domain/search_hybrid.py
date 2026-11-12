"""全局混合检索的稳定纯融合；来源身份独立，向量不代表引文事实。"""

import math
from dataclasses import dataclass

SEARCH_HYBRID_PROTOCOL = "global-search-hybrid-v1"


@dataclass(frozen=True)
class SearchSignal:
    kind: str
    paper_id: str
    id: str
    bm25: float | None = None
    cosine: float | None = None
    input_hash: str | None = None


@dataclass(frozen=True)
class SearchRank:
    kind: str
    paper_id: str
    id: str
    bm25: float | None
    cosine: float | None
    text_contribution: float
    cosine_contribution: float
    total: float
    input_hash: str | None


def _validate(signal: SearchSignal) -> None:
    for value in (signal.bm25, signal.cosine):
        if value is not None and (
            isinstance(value, bool)
            or not isinstance(value, (int, float))
            or not math.isfinite(value)
        ):
            raise ValueError("检索信号必须为有限数值")
    if signal.bm25 is not None and signal.bm25 > 0:
        raise ValueError("词法BM25必须非正")
    if signal.cosine is not None and not -1 <= signal.cosine <= 1:
        raise ValueError("余弦必须介于负一与一之间")


def _merge(signals: tuple[SearchSignal, ...]) -> tuple[SearchSignal, ...]:
    groups: dict[tuple[str, str, str], list[SearchSignal]] = {}
    for signal in signals:
        _validate(signal)
        groups.setdefault((signal.kind, signal.paper_id, signal.id), []).append(signal)
    merged = []
    for (kind, paper_id, id_), rows in groups.items():
        lexical = [row.bm25 for row in rows if row.bm25 is not None]
        vectors = [row for row in rows if row.cosine is not None]
        vector = (
            max(vectors, key=lambda row: (row.cosine, row.input_hash or "")) if vectors else None
        )
        merged.append(
            SearchSignal(
                kind,
                paper_id,
                id_,
                min(lexical) if lexical else None,
                vector.cosine if vector else None,
                vector.input_hash if vector else None,
            )
        )
    return tuple(merged)


def contribution(signal: SearchSignal, maximum: float) -> SearchRank:
    """同一候选池的最大词法强度归一化；全零短词命中仍记一分。"""
    text = 0.0
    if signal.bm25 is not None:
        text = -signal.bm25 / maximum if maximum > 0 else 1.0
    vector = max(0.0, signal.cosine) if signal.cosine is not None else 0.0
    return SearchRank(
        signal.kind,
        signal.paper_id,
        signal.id,
        signal.bm25,
        signal.cosine,
        text,
        vector,
        math.fsum((text, vector)),
        signal.input_hash,
    )


def fuse(signals: tuple[SearchSignal, ...]) -> tuple[SearchRank, ...]:
    merged = _merge(signals)
    maximum = max((-row.bm25 for row in merged if row.bm25 is not None), default=0.0)
    ranked = (contribution(row, maximum) for row in merged)
    return tuple(
        sorted(
            ranked,
            key=lambda row: (
                -row.total,
                row.bm25 or 0.0,
                row.kind,
                row.paper_id,
                row.id,
            ),
        )
    )
