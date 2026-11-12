"""已有文本索引的有效命中：先排除失效来源，再取每篇最强分数。"""

import math
from collections import defaultdict
from collections.abc import Mapping

from litgleaner.domain.memory import FactSignals
from litgleaner.domain.memory_candidate_views import TextEvidence
from litgleaner.ports.memory_candidates import CandidateSnapshot, CandidateTextHit


def _usable(
    hit: CandidateTextHit,
    data: CandidateSnapshot,
    extractions: Mapping[str, str],
) -> bool:
    if not math.isfinite(hit.bm25) or hit.bm25 >= 0:
        return False
    if hit.kind == "paper":
        return hit.source_id == hit.paper_id
    if hit.kind == "extraction":
        return extractions.get(hit.source_id) == hit.paper_id
    if hit.kind == "fulltext":
        state = data.facts.fulltexts.get(hit.paper_id)
        return (
            state is not None and state.doc_key is not None and hit.source_version == state.doc_key
        )
    return False


def text_signals(
    paper_id: str,
    data: CandidateSnapshot,
    signals: Mapping[str, FactSignals],
) -> tuple[dict[str, float], dict[str, tuple[TextEvidence, ...]]]:
    library = {paper.id for paper in data.facts.papers}
    extractions = {
        str(item.id): item.paper_id for item in data.facts.extractions if item.paper_id in signals
    }
    best: dict[tuple[str, str], CandidateTextHit] = {}
    for hit in data.text_hits:
        if (
            hit.paper_id not in library
            or hit.paper_id == paper_id
            or not _usable(hit, data, extractions)
        ):
            continue
        key = hit.paper_id, hit.kind
        found = best.get(key)
        if found is None or (hit.bm25, hit.source_id) < (found.bm25, found.source_id):
            best[key] = hit
    grouped: dict[str, list[TextEvidence]] = defaultdict(list)
    for (other, _kind), hit in sorted(best.items()):
        grouped[other].append(
            TextEvidence(hit.kind, hit.source_id, hit.page, hit.source_version, hit.bm25)
        )
    raw = {other: max(-hit.bm25 for hit in hits) for other, hits in grouped.items()}
    scale = max(raw.values(), default=1.0)
    return (
        {key: value / scale for key, value in raw.items()},
        {key: tuple(hits) for key, hits in grouped.items()},
    )
