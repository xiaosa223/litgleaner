"""检索词命中篇数（第 7.2 节）：按编辑表里尚未保存的检索词查近一年的篇数，不调用模型、不改文件。"""

import pytest
from fastapi.testclient import TestClient

from litgleaner.domain.errors import RateLimited, TransientError
from litgleaner.infra.config import load_config
from litgleaner.server.app import create_app
from litgleaner.server.security import LocalSecurity
from litgleaner.service import ModelRuntime
from tests.e2e.conftest import Harness, paper
from tests.e2e.test_model_settings import MemorySecrets

URL = "/api/topics/alpha-topic/term-counts"


def _client(harness: Harness) -> TestClient:
    runtime = ModelRuntime(load_config(harness.root / "config"), harness.ctx, MemorySecrets())
    app = create_app(
        harness.ctx,
        LocalSecurity(48154, launch_token="count-test"),
        None,
        context_provider=runtime.snapshot,
        model_settings=runtime,
        topic_settings=runtime,
    )
    client = TestClient(app, base_url="http://127.0.0.1:48154")
    client.get("/launch", params={"token": "count-test"})
    client.headers["X-CSRF-Token"] = client.get("/api/session").json()["csrf_token"]
    return client


def _counting(data):
    data["sources"]["alpha"]["options"]["term_counts"] = True


@pytest.fixture
def client(harness: Harness) -> TestClient:
    harness.edit_yaml("sources.yaml", _counting)
    harness.rebuild()
    titles = ["alpha graphs", "Alpha signals", "beta only", "alpha fMRI"]
    harness.write_papers([paper(t, abstract=f"About {t}.") for t in titles])
    return _client(harness)


def test_counts_each_term_with_its_synonyms_and_the_exclusions(client, harness):
    body = {
        "include": [{"term": "alpha", "synonyms": []}, {"term": "beta", "synonyms": ["gamma"]}],
        "exclude": ["fMRI"],
    }
    response = client.post(URL, json=body)
    assert response.status_code == 200, response.text
    data = response.json()
    assert data["source"] == "alpha" and data["until"] > data["since"]
    assert [(row["term"], row["hits"], row["error"]) for row in data["counts"]] == [
        ("alpha", 2, None),
        ("beta", 1, None),
    ]
    assert harness.llm_calls() == 0
    files = sorted(p.name for p in (harness.root / "config" / "topics").iterdir())
    assert files == ["alpha-topic.yaml"]


def test_one_failing_term_does_not_hide_the_others(client, harness):
    harness.ctx.sources["alpha"].fail_next.append(TransientError("busy"))
    body = {"include": [{"term": "alpha", "synonyms": []}, {"term": "beta", "synonyms": []}]}
    counts = client.post(URL, json=body).json()["counts"]
    assert counts[0]["hits"] is None and counts[0]["error"] == "source_unavailable"
    assert counts[1]["hits"] == 1


def test_without_a_counting_source_says_so(harness):
    response = _client(harness).post(URL, json={"include": [{"term": "a", "synonyms": []}]})
    assert response.status_code == 422 and response.json()["detail"] == "term_count_unavailable"


@pytest.mark.parametrize(
    "body",
    [
        {"include": []},
        {"include": [{"term": "a", "synonyms": []}] * 21},
        {"include": [{"term": "a", "synonyms": ["s"] * 9}]},
        {"include": [{"term": "a", "synonyms": []}], "extra": 1},
    ],
)
def test_invalid_requests_are_rejected(client, body):
    assert client.post(URL, json=body).status_code == 422


def test_rate_limit_stops_the_remaining_requests(client, harness):
    source = harness.ctx.sources["alpha"]
    calls = []
    original = source.fetch
    source.fetch = lambda query, cursor: calls.append(query) or original(query, cursor)
    source.fail_next.append(RateLimited("slow down"))
    body = {"include": [{"term": t, "synonyms": []} for t in ("alpha", "beta", "gamma")]}
    counts = client.post(URL, json=body).json()["counts"]
    assert [row["hits"] for row in counts] == [None, None, None]
    assert {row["error"] for row in counts} == {"source_unavailable"}
    assert len(calls) == 1


def test_an_invalid_row_is_rejected_before_any_request(client, harness):
    source = harness.ctx.sources["alpha"]
    calls = []
    source.fetch = lambda query, cursor: calls.append(query)
    body = {"include": [{"term": "alpha", "synonyms": []}, {"term": "   ", "synonyms": []}]}
    response = client.post(URL, json=body)
    assert response.status_code == 422 and response.json()["detail"] == "query_invalid"
    assert calls == []
