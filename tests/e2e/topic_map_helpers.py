"""地图验收的离线模型与当前课题事实。"""

from litgleaner.application.stages.discover import resolve_record
from litgleaner.domain.paper import SourceRecord
from litgleaner.ports.store import TriageResult
from tests.e2e.conftest import Harness

TOPIC = "alpha-topic"


def setup_map(harness: Harness, count: int = 7):
    harness.edit_yaml(
        "llm.yaml",
        lambda data: data["tasks"].update(
            {"map": {"provider": "main", "model": "demo-map", "prompt": "map"}}
        ),
    )
    ctx = harness.rebuild()
    ids = tuple(add_paper(harness, f"Paper alpha {n}") for n in range(count))
    return ctx, ids


def add_paper(
    harness: Harness, title: str, *, relevant: bool = True, abstract: str = "alpha research"
):
    ctx = harness.ctx
    record = SourceRecord(
        title=title, abstract=abstract, doi=f"10.1000/{title.replace(' ', '-').lower()}"
    )
    with ctx.store.atomic():
        id_ = resolve_record(ctx.store.papers, record, ctx.now_iso())
        ctx.store.judgments.save_triage(
            TriageResult(
                id_, TOPIC, ctx.topics[TOPIC].topic_hash, relevant, "fixture", "fixture", "fixture"
            ),
            ctx.now_iso(),
        )
    return id_
