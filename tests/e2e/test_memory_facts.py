"""文库事实关系批量读取、指纹护栏、来源和本地 API 安全合同。"""

from dataclasses import replace

import pytest
from fastapi.testclient import TestClient

from litgleaner.application import memory
from litgleaner.application.documents import load_document, save_document
from litgleaner.application.pipeline import run_stages
from litgleaner.domain.document import Block
from litgleaner.domain.errors import InvalidRequest, NotFound
from litgleaner.domain.identity import Alias
from litgleaner.domain.knowledge import ConceptOccurrence
from litgleaner.server.app import create_app
from litgleaner.server.security import LocalSecurity
from tests.e2e.conftest import extraction_answer, paper
from tests.e2e.memory_helpers import QUOTE, add, citation, extract, no_extract_route


def test_shared_current_facts_keep_both_sides_and_original_provenance(harness):
    a, b = add(harness, "a"), add(harness, "b")
    left, right = extract(harness, a), extract(harness, b)
    result = memory.facts(harness.ctx, a)
    assert result.total == 1 and not result.warnings
    (match,) = result.matches
    assert match.paper.paper_id == b and match.paper.reading_status == "queued"
    (shared,) = match.core_concepts
    assert shared.name == "Transformer" and shared.here.paper_id == a
    assert shared.there.paper_id == b and shared.here.definition is None
    assert not shared.here.verified  # 未核验的定义没有被补成“定义事实”。
    for field in (match.datasets[0], match.methods[0]):
        assert field.here[0].quote == QUOTE and field.here[0].page == 2
        assert field.here[0].extraction_id == left.id
        assert field.there[0].extraction_id == right.id
        assert field.here[0].input_hash == left.input_hash
        assert field.here[0].producer_version == left.extractor_version


def test_real_extract_pipeline_supplies_memory_without_a_second_model_call(harness):
    harness.write_papers([paper("Memory A", 5), paper("Memory B", 5)])
    assert run_stages(harness.ctx, ("discover", "triage", "acquire", "parse")).ok
    ids = harness.ctx.store.papers.parsed_without_extraction()
    evidence = {"text": "Dataset X", "quote": QUOTE, "page": 0}
    harness.llm.overrides["extract"] = {
        **extraction_answer(),
        "datasets": [evidence],
        "method": {**evidence, "text": "Attention"},
        "key_concepts": [{"name": "Transformer", "role": "core", "definition": None, "page": 0}],
    }
    for paper_id in ids:
        state = harness.ctx.store.papers.get_fulltext(paper_id)
        doc = load_document(harness.ctx, state.doc_key)
        save_document(
            harness.ctx,
            replace(
                doc,
                blocks=(
                    Block("heading", "Abstract", 0),
                    Block("text", "Transformer. " + QUOTE, 0),
                ),
            ),
        )
        harness.ctx.store.reader.set_state(paper_id, "reading", None, harness.ctx.now_iso())
    assert harness.run("extract").ok and harness.llm_calls("extract") == 2
    result = memory.facts(harness.ctx, ids[0])
    assert result.total == 1 and not result.warnings
    assert result.matches[0].core_concepts and result.matches[0].datasets
    assert result.matches[0].methods and harness.llm_calls("extract") == 2


@pytest.mark.parametrize(
    "change", ["language", "budget", "chunker", "doc", "prompt", "model", "provider"]
)
def test_stale_extractions_never_become_current_facts_but_citations_survive(harness, change):
    a, b = add(harness, "a"), add(harness, "b")
    extract(harness, a)
    extract(harness, b)
    evidence = citation(harness, a, target=b)
    ctx = harness.ctx
    if change in {"language", "budget", "chunker"}:
        changes = {
            "language": {"reading_language": "en"},
            "budget": {"assess_token_budget": 5999},
            "chunker": {"chunker": replace(ctx.settings.chunker, target_tokens=222)},
        }
        ctx = replace(ctx, settings=replace(ctx.settings, **changes[change]))
    elif change == "doc":
        state = ctx.store.papers.get_fulltext(a)
        ctx.store.papers.save_fulltext(replace(state, doc_key="blob://another-doc"), ctx.now_iso())
    else:
        route = ctx.llm.routes["extract"]
        if change == "prompt":
            path = harness.root / "prompts/extract.md"
            path.write_text(path.read_text() + "\nChanged rule\n")
            ctx = harness.rebuild()
        else:
            values = {"model": {"model": "changed"}, "provider": {"provider_version": "changed"}}
            ctx = replace(
                ctx,
                llm=replace(
                    ctx.llm, routes={**ctx.llm.routes, "extract": replace(route, **values[change])}
                ),
            )
    result = memory.facts(ctx, a)
    (match,) = result.matches
    assert match.cites == (evidence,)
    assert not (match.datasets or match.methods or match.core_concepts)
    expected = "stale_producer" if change in {"prompt", "model", "provider"} else "stale_input"
    assert expected in {w.code for w in result.warnings if w.paper_id == a}


def test_missing_route_keeps_existing_facts_and_explicit_unchecked_diagnostics(harness):
    a, b = add(harness, "a"), add(harness, "b")
    extract(harness, a)
    extract(harness, b)
    no_extract_route(harness)
    result = memory.facts(harness.ctx, a)
    assert result.total == 1 and result.matches[0].datasets
    assert {w.paper_id for w in result.warnings if w.code == "producer_unchecked"} == {a, b}


def test_latest_bad_extraction_is_used_without_falling_back_to_old_facts(harness):
    a, b = add(harness, "a"), add(harness, "b")
    extract(harness, a)
    extract(harness, b)
    extract(harness, a, input_hash="outdated")
    result = memory.facts(harness.ctx, a)
    assert result.total == 0
    assert [(w.paper_id, w.code) for w in result.warnings] == [(a, "stale_input")]


def test_first_occurrence_must_match_latest_extraction_fingerprints(harness):
    a, b = add(harness, "a"), add(harness, "b")
    concept = harness.ctx.store.knowledge.register("Transformer", None, harness.ctx.now_iso())
    harness.ctx.store.knowledge.add_occurrence(
        ConceptOccurrence(
            concept.id, a, "core", None, 1, None, False, "old", "old", harness.ctx.now_iso()
        )
    )
    extract(harness, a)
    extract(harness, b)
    result = memory.facts(harness.ctx, a)
    assert result.total == 1 and result.matches[0].datasets
    assert not result.matches[0].core_concepts
    assert any(w.code == "stale_concept" and w.paper_id == a for w in result.warnings)


def test_registered_concept_alias_is_a_precise_current_core_name(harness):
    a, b = add(harness, "a"), add(harness, "b")
    extract(
        harness,
        a,
        data={
            "datasets": [],
            "method": None,
            "key_concepts": [
                {"name": "Transformer", "role": "core", "page": 1},
            ],
        },
    )
    concept = harness.ctx.store.knowledge.resolve("Transformer")
    harness.ctx.store.knowledge.add_alias(concept.id, "变换器", "zh-CN", harness.ctx.now_iso())
    extract(
        harness,
        b,
        core="变换器",
        data={
            "datasets": [],
            "method": None,
            "key_concepts": [
                {"name": "变换器", "role": "core", "page": 1},
            ],
        },
    )
    result = memory.facts(harness.ctx, a)
    assert result.total == 1 and not result.warnings
    assert result.matches[0].core_concepts[0].concept_id == concept.id


@pytest.mark.parametrize(
    "data",
    [
        {},
        {"datasets": [{"text": "Dataset X", "quote": QUOTE, "page": 2}]},
        {"datasets": [{"text": "Dataset X", "quote": QUOTE, "page": True, "verified": True}]},
        {"datasets": [{"text": "Dataset X", "quote": "short", "page": 2, "verified": True}]},
        {"datasets": "Dataset X", "method": {"verified": True}},
    ],
)
def test_legacy_unverified_or_incomplete_evidence_never_supplies_shared_fields(harness, data):
    a, b = add(harness, "a"), add(harness, "b")
    extract(harness, a, core=None, data=data)
    extract(harness, b)
    assert memory.facts(harness.ctx, a).total == 0


@pytest.mark.parametrize("field", ["datasets", "method"])
def test_wrong_container_with_verified_evidence_is_not_a_shared_fact(harness, field):
    a, b = add(harness, "a"), add(harness, "b")
    evidence = {
        "text": "Dataset X" if field == "datasets" else "Attention",
        "quote": QUOTE,
        "page": 2,
        "verified": True,
    }
    extract(harness, a, core=None, data={field: evidence if field == "datasets" else [evidence]})
    extract(harness, b)
    result = memory.facts(harness.ctx, a)
    assert result.total == 0 and any(w.code == "legacy_fields" for w in result.warnings)


@pytest.mark.parametrize(
    "concepts", [None, [], [{"name": "Transformer", "role": "mentioned", "page": 1}]]
)
def test_core_occurrence_without_current_core_field_is_not_a_core_fact(harness, concepts):
    a, b = add(harness, "a"), add(harness, "b")
    extract(harness, a, data={"key_concepts": concepts, "datasets": [], "method": None})
    extract(harness, b)
    result = memory.facts(harness.ctx, a)
    assert result.total == 0 and any(w.code == "stale_concept" for w in result.warnings)


def test_missing_document_drops_extraction_signals_and_reports_reason(harness):
    a, b = add(harness, "a"), add(harness, "b")
    extract(harness, a)
    extract(harness, b)
    state = harness.ctx.store.papers.get_fulltext(a)
    harness.ctx.store.papers.save_fulltext(replace(state, doc_key=None), harness.ctx.now_iso())
    result = memory.facts(harness.ctx, a)
    assert result.total == 0 and any(w.code == "missing_document" for w in result.warnings)


def test_candidate_papers_do_not_form_relationship_endpoints(harness):
    a, b = add(harness, "a"), add(harness, "b", library=False)
    extract(harness, a)
    extract(harness, b)
    citation(harness, a, target=b)
    citation(harness, b, target=a)
    assert memory.facts(harness.ctx, a).total == 0
    with pytest.raises(NotFound, match="文库"):
        memory.facts(harness.ctx, b)
    with pytest.raises(NotFound):
        memory.facts(harness.ctx, "missing")


def test_common_unresolved_target_counts_once_and_target_arrival_resolves_it(harness):
    a, b = add(harness, "a"), add(harness, "b")
    target = (Alias("doi", "10.1000/target"),)
    one = citation(harness, a, aliases=target)
    two = citation(harness, a, aliases=target, source="other", position="remote:0")
    other = citation(harness, b, aliases=target)
    (match,) = memory.facts(harness.ctx, a).matches
    (common,) = match.common_references
    assert common.target.paper_id is None and common.here == (two, one)
    assert common.there == (other,)
    target_id = add(harness, "target", library=False)
    (match,) = memory.facts(harness.ctx, a).matches
    assert len(match.common_references) == 1
    assert match.common_references[0].target.paper_id == target_id
    assert not match.cites  # 共同目标未在文库，不作为关系另一端。


def test_global_alias_conflicts_block_false_direct_links(harness):
    a, b = add(harness, "a"), add(harness, "b")
    add(harness, "outside", library=False)
    citation(harness, a, aliases=(Alias("doi", "10.1000/b"), Alias("doi", "10.1000/outside")))
    assert memory.facts(harness.ctx, a).total == 0
    citation(harness, a, target=b, aliases=(Alias("doi", "10.1000/outside"),), position="known")
    assert memory.facts(harness.ctx, a).matches[0].paper.paper_id == b


def test_query_only_uses_eight_select_snapshot_without_writes_models_or_sources(
    harness, monkeypatch
):
    a, b = add(harness, "a"), add(harness, "b")
    extract(harness, a)
    extract(harness, b)
    db = harness.ctx.store._db
    queries = []
    fetch = db.fetch

    def captured(statement):
        queries.append(statement)
        return fetch(statement)

    def forbidden(*_args, **_kwargs):
        raise AssertionError("read-only memory query attempted an external action or write")

    monkeypatch.setattr(db, "fetch", captured)
    monkeypatch.setattr(db, "write", forbidden)
    monkeypatch.setattr(type(harness.ctx.llm), "call", forbidden)
    monkeypatch.setattr(type(harness.ctx.blobs), "put", forbidden)
    for source in harness.ctx.sources.values():
        monkeypatch.setattr(type(source), "fetch", forbidden)
    assert memory.facts(harness.ctx, a).total == 1
    assert len(queries) == 8 and all(q.is_select for q in queries)


def test_api_has_complete_stable_paging_session_and_validation(harness):
    a = add(harness, "a")
    with harness.ctx.store.atomic():
        others = sorted(add(harness, f"neighbor-{n:02}") for n in range(55))
        for other in others:
            citation(harness, a, target=other, position=other)
    web = create_app(harness.ctx, LocalSecurity(48143, launch_token="memory-test"), None)
    base = f"/api/papers/{a}/memory-facts"
    with TestClient(web, base_url="http://127.0.0.1:48143") as client:
        assert client.get(base).status_code == 401
        client.get("/launch", params={"token": "memory-test"})
        default = client.get(base).json()
        assert default["total"] == 55 and len(default["matches"]) == 50
        pages = [
            client.get(base, params={"limit": 20, "offset": offset}).json()
            for offset in (0, 20, 40)
        ]
        assert all(page["total"] == 55 for page in pages)
        assert [item["paper"]["paper_id"] for page in pages for item in page["matches"]] == others
        assert client.get(base, params={"offset": 100}).json()["matches"] == []
        for params in ({"limit": 0}, {"limit": 201}, {"offset": -1}, {"limit": "bad"}):
            assert client.get(base, params=params).status_code == 422
        assert client.get("/api/papers/missing/memory-facts").status_code == 404
        assert client.get(base, headers={"Origin": "https://other.test"}).status_code == 403
        assert client.get(base, headers={"Host": "other.test"}).status_code == 400


@pytest.mark.parametrize("args", [{"limit": 0}, {"limit": 201}, {"offset": -1}])
def test_application_rejects_invalid_paging(harness, args):
    with pytest.raises(InvalidRequest):
        memory.facts(harness.ctx, "missing", **args)
