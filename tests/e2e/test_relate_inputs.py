"""冻结前先排除不合格双端，预算和来源指纹覆盖实际完整输入。"""

import json
from collections import Counter
from dataclasses import replace

import pytest
from sqlalchemy import update

from litgleaner.application import relate_input
from litgleaner.application.codec import compress_json
from litgleaner.application.documents import load_document
from litgleaner.application.qa_context import qa_token_count
from litgleaner.domain.errors import InvalidRequest, NotFound
from litgleaner.domain.evidence import wrap_untrusted
from litgleaner.domain.identity import Alias
from litgleaner.domain.reading import NewAnnotation
from litgleaner.infra.db.schema import extraction as extraction_table
from tests.e2e.memory_helpers import add, citation
from tests.e2e.relate_helpers import HERE, THERE, configure, pair, source
from tests.e2e.test_memory_candidates import abstract, policy


def test_prepares_real_quote_fragments_without_notes_or_unsent_text_and_without_actions(
    harness, monkeypatch
):
    a, b = pair(harness)
    harness.ctx.store.reader.add_annotation(
        NewAnnotation(a, "note", None, None, None, "PRIVATE_USER_NOTE"), harness.ctx.now_iso()
    )
    original_fetch, queries = harness.ctx.store._db.fetch, []

    def capture(statement):
        queries.append(statement)
        return original_fetch(statement)

    def forbidden(*args, **kwargs):
        raise AssertionError("read-only preparation performed an action")

    monkeypatch.setattr(harness.ctx.store._db, "write", forbidden)
    monkeypatch.setattr(harness.ctx.store._db, "fetch", capture)
    monkeypatch.setattr(type(harness.llm), "complete", forbidden)
    prepared = relate_input.prepare_input(harness.ctx, a)
    data = json.loads(prepared.text)
    assert prepared.pairs_count == len(data["pairs"]) == 1
    assert set(prepared.paper_ids) == set(prepared.source_hashes) == {a, b}
    assert prepared.considered_ids == (a, b) and prepared.eligible_ids == (b,)
    assert HERE in prepared.text and THERE in prepared.text
    assert "PRIVATE_USER_NOTE" not in prepared.text and "secret tail" not in prepared.text
    assert prepared.tokens == qa_token_count(prepared.system + wrap_untrusted(prepared.text))
    assert 0 < prepared.estimated_usd < 0.1 and not harness.llm.calls
    assert len(queries) == 11


def test_filtered_endpoints_do_not_consume_pair_limit_or_change_fts_normalization(harness):
    configure(harness)
    a, b, c = (add(harness, key) for key in ("anchor", "not-admitted", "eligible"))
    for key, quote in ((a, HERE), (b, THERE), (c, THERE)):
        source(harness, key, quote)
        abstract(harness, key, "rare_search_token" if key != b else "rare_search_token " * 40)
    citation(harness, a, target=b)
    policy(harness, max_pairs=1, minimum_score=9, weights={"text": 10})
    prepared = relate_input.prepare_input(harness.ctx, a, eligible_ids=(c,))
    assert prepared.considered_ids == (a, c) and prepared.paper_ids == tuple(sorted((a, c)))
    assert b not in prepared.text and prepared.eligible_ids == (c,)


@pytest.mark.parametrize("change", ["stale-input", "stale-producer", "missing-blob", "no-quote"])
def test_current_or_readable_evidence_filter_runs_before_candidate_limit(harness, change):
    a, b = pair(harness)
    c = add(harness, "third")
    source(harness, c, THERE)
    citation(harness, a, target=c, position=c)
    policy(harness, max_pairs=1)
    kwargs = {"input_hash": "old"} if change == "stale-input" else {}
    kwargs |= {"producer": "old"} if change == "stale-producer" else {}
    kwargs |= {"doc_key": "missing-blob"} if change == "missing-blob" else {}
    kwargs |= {"fields": {"problem": None}} if change == "no-quote" else {}
    source(harness, b, THERE, **kwargs)
    prepared = relate_input.prepare_input(harness.ctx, a)
    assert prepared.considered_ids == (a, c)
    assert set(prepared.paper_ids) == {a, c} and b in prepared.eligible_ids
    assert any(b in warning for warning in prepared.warnings)


def test_budget_omits_whole_pair_and_preserves_considered_pool(harness):
    a, b = pair(harness)
    first = relate_input.prepare_input(harness.ctx, a)
    configure(harness, token_budget=first.tokens - 1)
    limited = relate_input.prepare_input(harness.ctx, a)
    assert limited.pairs_count == 0 and limited.paper_ids == () and limited.source_hashes == {}
    assert limited.considered_ids == (a, b) and limited.eligible_ids == (b,)
    assert json.loads(limited.text) == {"pairs": []}
    assert limited.tokens <= harness.ctx.settings.relate.token_budget
    assert any("整对" in warning for warning in limited.warnings)


def test_oversized_prompt_rejected_before_loading_sources(harness, monkeypatch):
    a, _ = pair(harness)
    route = harness.ctx.llm.routes["relate"]
    long_prompt = replace(route.prompt, text="界" * 10000)
    harness.ctx = replace(
        harness.ctx,
        llm=replace(harness.ctx.llm, routes={"relate": replace(route, prompt=long_prompt)}),
    )
    with pytest.raises(InvalidRequest, match="提示词超过"):
        relate_input.prepare_input(harness.ctx, a)


def test_actual_continuous_prefix_is_limited_and_counts_unicode_whole_message(harness):
    configure(harness, fragment_chars=100)
    a, b = add(harness, "unicode-anchor"), add(harness, "unicode-neighbor")
    quote = "原论文中的连续中文引文和数字说明，绝不把未发送的尾部当成依据。" * 10
    for key in (a, b):
        source(harness, key, quote)
    citation(harness, a, target=b)
    prepared = relate_input.prepare_input(harness.ctx, a)
    assert prepared.pairs_count == 1
    assert all(
        len(item.text) <= 100 for src in prepared.sources.values() for item in src.source.sources
    )
    assert quote not in prepared.text and quote[:100] in prepared.text
    assert prepared.tokens <= harness.ctx.settings.relate.token_budget


def test_distinct_document_pointer_read_once_and_bad_archive_isolated(harness, monkeypatch):
    a, b = pair(harness)
    document_key = harness.ctx.store.papers.get_fulltext(a).doc_key
    document = load_document(harness.ctx, document_key)
    c, d = add(harness, "same-document"), add(harness, "broken-document")
    source(harness, c, HERE, doc_key=document_key)
    source(harness, d, HERE, doc_key="missing-blob")
    for key in (c, d):
        citation(harness, a, target=key, position=key)
    original, calls = type(harness.ctx.blobs).get, []

    def capture(self, key):
        calls.append(key)
        return original(self, key)

    monkeypatch.setattr(type(harness.ctx.blobs), "get", capture)
    prepared = relate_input.prepare_input(harness.ctx, a)
    assert Counter(calls)[document_key] == Counter(calls)["missing-blob"] == 1
    assert set(prepared.paper_ids) == {a, b, c} and d not in prepared.text
    assert document.blocks[0].text.startswith(HERE)


def test_current_extraction_without_route_usable_with_explicit_unchecked_warning(harness):
    a, b = pair(harness)
    routes = {name: value for name, value in harness.ctx.llm.routes.items() if name != "extract"}
    harness.ctx = replace(harness.ctx, llm=replace(harness.ctx.llm, routes=routes))
    prepared = relate_input.prepare_input(harness.ctx, a)
    assert set(prepared.paper_ids) == {a, b}
    assert any("生产者未完整核验" in warning for warning in prepared.warnings)


def test_configuration_excludes_budget_caps_and_source_hash_tracks_unsent_extraction(harness):
    a, b = pair(harness)
    before = relate_input.prepare_input(harness.ctx, a)
    route = harness.ctx.llm.routes["relate"]
    harness.ctx = replace(
        harness.ctx,
        llm=replace(
            harness.ctx.llm,
            daily_budget_usd=999,
            routes={**harness.ctx.llm.routes, "relate": replace(route, daily_budget_usd=0)},
        ),
    )
    after_cap = relate_input.prepare_input(harness.ctx, a)
    assert (
        before.input_hash == after_cap.input_hash
        and before.producer_version == after_cap.producer_version
    )
    extraction = harness.ctx.store.papers.get_extraction(b)
    harness.ctx.store._db.write(
        update(extraction_table)
        .where(extraction_table.c.id == extraction.id)
        .values(data=json.dumps({**extraction.data, "summary": "changed unsent summary"}))
    )
    after_source = relate_input.prepare_input(harness.ctx, a)
    assert before.text == after_source.text
    assert before.source_hashes[b] != after_source.source_hashes[b]
    assert before.input_hash != after_source.input_hash


def test_invalid_library_anchor_rejected(harness):
    configure(harness)
    outside = add(harness, "outside", library=False)
    with pytest.raises(NotFound):
        relate_input.prepare_input(harness.ctx, outside)


def test_true_citation_context_localizes_while_external_context_does_not_become_evidence(harness):
    a, b = pair(harness)
    source(harness, a, HERE, fields={"problem": None})
    citation(
        harness, a, target=b, position="real-context", contexts=(HERE, "Fabricated source context")
    )
    prepared = relate_input.prepare_input(harness.ctx, a)
    assert prepared.pairs_count == 1
    assert prepared.sources[a].record["fragments"] == [
        {"field": "citation", "page": 2, "quote": HERE}
    ]


def test_user_decided_pair_filtered_before_it_can_take_max_pairs(harness, monkeypatch):
    a, b = pair(harness)
    c = add(harness, "unpinned")
    source(harness, c, THERE)
    citation(harness, a, target=c, position=c)
    policy(harness, max_pairs=1)
    monkeypatch.setattr(
        type(harness.ctx.store.relations),
        "user_decided_pairs",
        lambda self: frozenset({tuple(sorted((a, b)))}),
    )
    prepared = relate_input.prepare_input(harness.ctx, a)
    assert prepared.considered_ids == (a, c)
    assert set(prepared.eligible_ids) == {b, c} and b not in prepared.text


def test_filtered_library_scope_preserves_global_alias_conflict(harness):
    configure(harness)
    a, b = add(harness, "a"), add(harness, "b")
    add(harness, "outside", library=False)
    for key, quote in ((a, HERE), (b, THERE)):
        source(harness, key, quote)
    citation(harness, a, aliases=(Alias("doi", "10.1000/b"), Alias("doi", "10.1000/outside")))
    prepared = relate_input.prepare_input(harness.ctx, a, eligible_ids=(b,))
    assert prepared.eligible_ids == (b,)
    assert prepared.pairs_count == 0 and prepared.considered_ids == (a,)


def test_latest_stale_extraction_does_not_fall_back_to_matching_old_history(harness):
    a, b = pair(harness)
    old_state = harness.ctx.store.papers.get_fulltext(b)
    source(harness, b, THERE, doc_key=old_state.doc_key, input_hash="stale-latest")
    prepared = relate_input.prepare_input(harness.ctx, a)
    assert prepared.eligible_ids == (b,) and prepared.pairs_count == 0


@pytest.mark.parametrize(
    "damage", ["compressed", "shape", "caption", "boolean-page", "negative-page"]
)
def test_corrupt_archives_are_isolated_and_cached_per_pointer(harness, monkeypatch, damage):
    a, b = pair(harness)
    c, d = add(harness, "corrupt-one"), add(harness, "corrupt-two")
    for key in (c, d):
        source(harness, key, HERE, doc_key="broken-doc")
        citation(harness, a, target=key, position=key)
    data = load_document(harness.ctx, harness.ctx.store.papers.get_fulltext(a).doc_key).to_dict()
    if damage == "shape":
        data = {"blocks": []}
    elif damage in ("caption", "boolean-page", "negative-page"):
        data["blocks"][0].update(
            {"caption": True}
            if damage == "caption"
            else {"page": True if damage == "boolean-page" else -1}
        )
    harness.ctx.blobs.put(
        "broken-doc", b"damaged compression" if damage == "compressed" else compress_json(data)
    )
    original, reads = type(harness.ctx.blobs).get, []

    def get(self, key):
        reads.append(key)
        return original(self, key)

    monkeypatch.setattr(type(harness.ctx.blobs), "get", get)
    prepared = relate_input.prepare_input(harness.ctx, a)
    assert set(prepared.paper_ids) == {a, b}
    assert reads.count("broken-doc") == 1


def test_missing_model_and_unknown_price_do_not_call_model_or_guess_zero_fee(harness, monkeypatch):
    a, b = pair(harness)
    monkeypatch.setattr(type(harness.llm), "price", lambda self, model: None)
    unknown = relate_input.prepare_input(harness.ctx, a)
    assert unknown.estimated_usd is None and any("未提供价格" in w for w in unknown.warnings)
    harness.ctx = replace(
        harness.ctx,
        llm=replace(
            harness.ctx.llm,
            routes={
                name: route for name, route in harness.ctx.llm.routes.items() if name != "relate"
            },
        ),
    )
    missing = relate_input.prepare_input(harness.ctx, a)
    assert missing.producer_version is None and missing.estimated_usd is None
    assert set(missing.paper_ids) == {a, b} and harness.llm_calls("relate") == 0
