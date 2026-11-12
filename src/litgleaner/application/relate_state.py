"""确认写事务仅核对数据库状态，不在写锁内重复读文档归档。"""

import json
from dataclasses import asdict, replace
from typing import Any

from litgleaner.application.context import AppContext
from litgleaner.application.relate_input import configuration
from litgleaner.domain.hashing import stable_hash


def state_token(ctx: AppContext) -> str:
    with ctx.store.atomic():
        facts = ctx.store.memory_snapshot()
        vectors = ctx.store.embeddings.snapshot().embeddings
        decided = ctx.store.relations.user_decided_pairs()
    # 阅读位置不改变候选；文库成员已由facts.papers精确表达。
    content: dict[str, Any] = {
        "facts": asdict(replace(facts, reading={})),
        "vectors": [(row.paper_id, row.input_hash, row.embedder_version) for row in vectors],
        "decided": sorted(decided),
        "configuration": configuration(ctx),
    }
    normalized = json.loads(
        json.dumps(content, default=lambda model: model.model_dump(mode="json"))
    )
    return stable_hash(normalized)
