"""参考文献只读事实与显式网络刷新，不创建候选论文或模型任务。"""

from dataclasses import replace

import pytest
from fastapi.testclient import TestClient

from litgleaner.domain.citation import ReferenceRecord
from litgleaner.domain.errors import SchemaChanged, TransientError
from litgleaner.domain.identity import Alias
from litgleaner.domain.paper import SourceRecord
from litgleaner.domain.query import ReferenceList
from litgleaner.ports.source import FetchResult
from litgleaner.server.app import create_app
from litgleaner.server.security import LocalSecurity
from tests.e2e.conftest import Harness, paper


class ReferenceSource:
    name = "references"
    capabilities = frozenset({ReferenceList})

    def __init__(self, citing_doi: str):
        self.calls = 0
        self.error = None
        self.result = FetchResult(
            (SourceRecord(title="Unseen paper", doi="10.1234/unseen"),),
            None,
            warnings=("只返回本次上限内的结果",),
            raw=b'{"references":"simulated response"}',
            truncated=True,
            references=(
                ReferenceRecord(
                    citing_doi,
                    0,
                    "Original target citation",
                    "Reference Target",
                    (Alias("doi", "10.1000/reference-target"),),
                    ("We compare with this method.",),
                    ("result_comparison",),
                    True,
                ),
                ReferenceRecord(citing_doi, 1, "Unidentified original entry", None, ()),
            ),
        )

    def fetch(self, query, cursor):
        assert isinstance(query, ReferenceList) and cursor is None
        self.calls += 1
        if self.error:
            raise self.error
        return self.result


@pytest.fixture
def citation_ui(harness: Harness):
    harness.write_papers([paper("Reference Parent", 5), paper("Reference Target", 5)])
    assert harness.run().ok
    ids = {
        harness.ctx.store.papers.get(i).title: i
        for i in harness.ctx.store.judgments.papers_with_scores()
    }
    parent, target = ids["Reference Parent"], ids["Reference Target"]
    source = ReferenceSource("10.1000/reference-parent")
    harness.ctx = replace(harness.ctx, sources={**harness.ctx.sources, source.name: source})
    with TestClient(
        create_app(harness.ctx, LocalSecurity(48133, launch_token="citation-test"), None),
        base_url="http://127.0.0.1:48133",
    ) as client:
        client.get("/launch", params={"token": "citation-test"})
        csrf = {"X-CSRF-Token": client.get("/api/session").json()["csrf_token"]}
        yield harness, client, csrf, source, parent, target


def _refresh(client, csrf, paper_id):
    return client.post(
        f"/api/papers/{paper_id}/references/refresh", json={"sources": ["references"]}, headers=csrf
    )


def test_reference_refresh_preserves_facts_and_is_idempotent_without_model_calls(citation_ui):
    harness, client, csrf, source, parent, target = citation_ui
    before = harness.llm_calls()
    result = _refresh(client, csrf, parent)
    assert result.status_code == 200
    report = result.json()["sources"][0]
    assert report["fetched"] == report["added"] == 2
    assert report["candidates"] == 1 and report["truncated"]
    assert report["warnings"] == ["只返回本次上限内的结果"]
    repeated = _refresh(client, csrf, parent).json()["sources"][0]
    assert repeated["added"] == 0 and source.calls == 2
    outgoing = client.get(f"/api/papers/{parent}/citations").json()["outgoing"]
    assert len(outgoing) == 2 and outgoing[0]["cited_paper_id"] == target
    assert outgoing[0]["citation"]["contexts"] == ["We compare with this method."]
    assert outgoing[0]["citation"]["intents"] == ["result_comparison"]
    assert outgoing[0]["citation"]["influential"] is True
    assert outgoing[1]["cited_paper_id"] is None
    incoming = client.get(f"/api/papers/{target}/citations").json()["incoming"]
    assert incoming[0]["citation"]["citing_paper_id"] == parent
    assert not harness.ctx.store.papers.find_ids_by_aliases([Alias("doi", "10.1234/unseen")])
    assert harness.llm_calls() == before


def test_citation_reads_never_fetch_or_generate_jobs(citation_ui):
    harness, client, _, source, parent, _ = citation_ui
    before = harness.llm_calls()
    for _ in range(2):
        response = client.get(f"/api/papers/{parent}/citations")
        assert response.status_code == 200 and response.json() == {"outgoing": [], "incoming": []}
    assert source.calls == 0 and harness.llm_calls() == before
    assert not harness.ctx.store.jobs.due("references", harness.ctx.now_iso())


def test_source_failure_is_reported_without_leaking_details_or_blocking_other_sources(citation_ui):
    harness, client, csrf, source, parent, _ = citation_ui
    failed = ReferenceSource("10.1000/reference-parent")
    failed.name = "failed-source"
    failed.error = TransientError("sensitive temporary details")
    harness.ctx.sources[failed.name] = failed
    response = client.post(
        f"/api/papers/{parent}/references/refresh",
        json={"sources": [failed.name, source.name]},
        headers=csrf,
    )
    assert response.status_code == 200
    reports = response.json()["sources"]
    assert reports[0]["error"] == "TransientError" and reports[1]["added"] == 2
    assert "sensitive" not in response.text


def test_bad_parent_metadata_cannot_attach_cross_paper_citations(citation_ui):
    _, client, csrf, source, parent, _ = citation_ui
    item = replace(source.result.references[0], citing_doi="10.1234/another-parent")
    source.result = replace(source.result, references=(item,))
    result = _refresh(client, csrf, parent).json()["sources"][0]
    assert result["error"] == SchemaChanged.__name__ and result["added"] == 0
    assert client.get(f"/api/papers/{parent}/citations").json()["outgoing"] == []


def test_reference_refresh_requires_csrf_and_valid_configured_sources(citation_ui):
    _, client, csrf, _, parent, _ = citation_ui
    path = f"/api/papers/{parent}/references/refresh"
    assert client.post(path, json={}).status_code == 403
    assert client.post(path, json={"sources": ["not-configured"]}, headers=csrf).status_code == 422
    assert client.post(path, json={"sources": []}, headers=csrf).status_code == 422
    assert _refresh(client, csrf, "missing").status_code == 404
    assert client.get("/api/papers/missing/citations").status_code == 404


def test_indexing_existing_archive_requires_csrf_and_stays_local(citation_ui):
    harness, client, csrf, source, parent, _ = citation_ui
    before = harness.llm_calls()
    path = f"/api/papers/{parent}/citations/index"
    assert client.post(path).status_code == 403
    assert client.post(path, headers=csrf).json() == {"outgoing": [], "incoming": []}
    assert source.calls == 0 and harness.llm_calls() == before
    assert client.post("/api/papers/missing/citations/index", headers=csrf).status_code == 404
