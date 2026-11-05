"""信号源达到单次上限时，游标不前进：被截掉的记录下次还能取到，而不是永久丢失。"""

from litfactory.application.stages.discover import DiscoverStage
from litfactory.domain.paper import SourceRecord
from litfactory.domain.query import KeywordSearch, Query
from litfactory.ports.source import FetchResult


class CappedSource:
    name = "alpha"
    capabilities: frozenset[type] = frozenset({KeywordSearch})

    def __init__(self, truncated: bool) -> None:
        self.truncated = truncated

    def fetch(self, query: Query, cursor: str | None) -> FetchResult:
        record = SourceRecord(title="Some paper about things", doi="10.1/capped")
        return FetchResult((record,), "2026-11-03", ("截断",), truncated=self.truncated)


def run_discover(harness, truncated: bool) -> str | None:
    harness.ctx.store.papers.set_cursor("alpha", QUERY.hash, "2026-10-01", "2026-10-01T00:00:00")
    harness.ctx.sources["alpha"] = CappedSource(truncated)
    DiscoverStage().process(
        harness.ctx, {"source": "alpha", "query": QUERY.model_dump(mode="json")}
    )
    return harness.ctx.store.papers.get_cursor("alpha", QUERY.hash)


QUERY = KeywordSearch(terms=("things",))


def test_truncated_fetch_keeps_records_but_not_the_cursor(harness):
    assert run_discover(harness, truncated=True) == "2026-10-01"
    assert harness.ctx.store.papers.find_by_title_key("somepaperaboutthings")


def test_complete_fetch_advances_the_cursor(harness):
    assert run_discover(harness, truncated=False) == "2026-11-03"
