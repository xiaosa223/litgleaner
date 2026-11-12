"""一次准备共用归档缓存，付费完成后另读最新文档逐对核验。"""

from collections import Counter
from dataclasses import replace

from litgleaner.application.codec import compress_json
from litgleaner.application.documents import load_document
from litgleaner.application.relate_input import (
    RelatePreparationCache,
    prepare_input,
    refresh_documents,
    validate_result,
)
from litgleaner.domain.document import Block
from tests.e2e.memory_helpers import add, citation
from tests.e2e.relate_helpers import HERE, THERE, answer, configure, pair, source


def capture_reads(harness, monkeypatch):
    original, reads = type(harness.ctx.blobs).get, []

    def get(self, key):
        reads.append(key)
        return original(self, key)

    monkeypatch.setattr(type(harness.ctx.blobs), "get", get)
    return reads


def test_shared_preparation_cache_reads_ten_distinct_docs_once_across_ten_inputs(
    harness, monkeypatch
):
    configure(harness)
    ids = tuple(add(harness, f"cache-paper-{index}") for index in range(10))
    for index, paper_id in enumerate(ids):
        source(harness, paper_id, HERE)
        for earlier in ids[:index]:
            citation(harness, paper_id, target=earlier, position=earlier)
    reads = capture_reads(harness, monkeypatch)
    cache = RelatePreparationCache()
    prepared = [
        prepare_input(harness.ctx, paper_id, ids[:index], cache=cache)
        for index, paper_id in enumerate(ids)
    ]
    assert len(reads) == len(set(reads)) == len(cache.documents) == 10
    assert all(value == 1 for value in Counter(reads).values())
    assert prepared[0].pairs_count == 0 and prepared[-1].pairs_count == 5
    second_cache = RelatePreparationCache()
    second = [
        prepare_input(harness.ctx, paper_id, ids[:index], cache=second_cache)
        for index, paper_id in enumerate(ids)
    ]
    assert len(reads) == 20
    assert [(p.text, p.input_hash) for p in prepared] == [(p.text, p.input_hash) for p in second]


def test_failed_document_reads_shared_once_and_repaired_blob_requires_fresh_cache(
    harness, monkeypatch
):
    a, b = pair(harness)
    c = add(harness, "unreadable-neighbor")
    source(harness, c, HERE, doc_key="missing-current-doc")
    citation(harness, a, target=c, position=c)
    reads = capture_reads(harness, monkeypatch)
    cache = RelatePreparationCache()
    first = prepare_input(harness.ctx, a, cache=cache)
    assert c not in first.paper_ids
    document = load_document(harness.ctx, first.sources[a].doc_key)
    harness.ctx.blobs.put("missing-current-doc", compress_json(document.to_dict()))
    still_failed = prepare_input(harness.ctx, a, cache=cache)
    assert c not in still_failed.paper_ids and reads.count("missing-current-doc") == 1
    repaired = prepare_input(harness.ctx, a, cache=RelatePreparationCache())
    assert set(repaired.paper_ids) == {a, b, c} and reads.count("missing-current-doc") == 2


def test_refresh_documents_reloads_each_current_pointer_and_keeps_frozen_identity(
    harness, monkeypatch
):
    a, b = pair(harness)
    prepared = prepare_input(harness.ctx, a)
    reads = capture_reads(harness, monkeypatch)
    refreshed = refresh_documents(harness.ctx, prepared)
    assert len(reads) == len(set(reads)) == 2
    assert refreshed.text == prepared.text and refreshed.input_hash == prepared.input_hash
    assert refreshed.source_hashes == prepared.source_hashes
    proposals, warnings = validate_result(
        harness.ctx, refreshed, {"relations": [answer(prepared, a, b)]}
    )
    assert len(proposals) == 1 and not warnings


def test_refresh_same_key_changed_original_rejects_stale_quote(harness, monkeypatch):
    a, b = pair(harness)
    prepared = prepare_input(harness.ctx, a)
    original = type(harness.ctx.blobs).get
    changed = replace(
        prepared.sources[b].document,
        blocks=(Block("text", "Changed current original document.", 2),),
    )

    def get(self, key):
        return (
            compress_json(changed.to_dict())
            if key == prepared.sources[b].doc_key
            else original(self, key)
        )

    monkeypatch.setattr(type(harness.ctx.blobs), "get", get)
    refreshed = refresh_documents(harness.ctx, prepared)
    assert b not in refreshed.sources
    proposals, warnings = validate_result(
        harness.ctx, refreshed, {"relations": [answer(prepared, a, b)]}
    )
    assert not proposals and any("当前原文" in warning for warning in warnings)


def test_refresh_bad_side_drops_affected_pair_and_preserves_other_good_pair(harness, monkeypatch):
    a, b = pair(harness)
    c = add(harness, "unaffected-pair")
    source(harness, c, THERE)
    citation(harness, a, target=c, position=c)
    prepared = prepare_input(harness.ctx, a)
    responses = [answer(prepared, a, b), answer(prepared, a, c)]
    original = type(harness.ctx.blobs).get

    def get(self, key):
        if key == prepared.sources[b].doc_key:
            raise OSError("archive unavailable after paid request")
        return original(self, key)

    monkeypatch.setattr(type(harness.ctx.blobs), "get", get)
    refreshed = refresh_documents(harness.ctx, prepared)
    assert set(refreshed.sources) == {a, c}
    proposals, warnings = validate_result(harness.ctx, refreshed, {"relations": responses})
    assert len(proposals) == 1 and proposals[0].paper_b == c and warnings


def test_refresh_shared_bad_pointer_is_read_once_and_all_dependent_pairs_drop(harness, monkeypatch):
    a, b = pair(harness)
    c = add(harness, "same-bad-pointer")
    source(harness, c, HERE, doc_key=harness.ctx.store.papers.get_fulltext(a).doc_key)
    citation(harness, a, target=c, position=c)
    prepared = prepare_input(harness.ctx, a)
    responses = [answer(prepared, a, b), answer(prepared, a, c, quote_b=HERE)]
    original, reads = type(harness.ctx.blobs).get, []

    def get(self, key):
        reads.append(key)
        if key == prepared.sources[a].doc_key:
            return b"invalid compressed archive"
        return original(self, key)

    monkeypatch.setattr(type(harness.ctx.blobs), "get", get)
    refreshed = refresh_documents(harness.ctx, prepared)
    assert reads.count(prepared.sources[a].doc_key) == 1 and set(refreshed.sources) == {b}
    proposals, warnings = validate_result(harness.ctx, refreshed, {"relations": responses})
    assert not proposals and len(warnings) == 2
