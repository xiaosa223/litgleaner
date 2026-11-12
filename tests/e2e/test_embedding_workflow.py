"""本地重算在真实文库/快照中追加，精确缓存、并发核对与相似度无模型副作用。"""

from dataclasses import replace

import pytest
from sqlalchemy import update

from litgleaner.application import embeddings
from litgleaner.application.embedding_inputs import inputs
from litgleaner.domain.embedding import EmbeddingBatch
from litgleaner.domain.errors import ConfigError, InvalidRequest, NotFound
from litgleaner.infra.db.schema import paper
from tests.e2e.embedding_helpers import setup
from tests.e2e.memory_helpers import add, extract, no_extract_route


def _input(harness, paper_id):
    data = harness.ctx.store.embeddings.snapshot()
    return inputs(harness.ctx, data.papers, data.fulltexts, data.extractions)[paper_id]


def test_preview_is_readonly_and_execute_reuses_exact_current_fingerprints(harness):
    model = setup(harness)
    a, b = add(harness, "a"), add(harness, "b")
    add(harness, "candidate", library=False)
    plan = embeddings.preview(harness.ctx)
    assert plan.total == 2 and plan.cached == 0
    assert set(plan.pending_paper_ids) == {a, b} and not model.calls
    result = embeddings.reembed(harness.ctx)
    assert result.created == 2 and result.reused == 0 and len(model.calls) == 1
    for key in (a, b):
        (saved,) = harness.ctx.store.embeddings.for_paper(key)
        assert saved.input_hash == _input(harness, key).input_hash
        assert saved.input_text == f"title: {'a' if key == a else 'b'}"
        assert saved.embedder_version == model.model.producer_version
    again = embeddings.reembed(harness.ctx)
    assert again.created == 0 and again.reused == 2 and len(model.calls) == 1
    assert embeddings.preview(harness.ctx).cached == 2
    assert harness.llm_calls() == 0


def test_empty_library_does_not_load_or_prepare_model(harness):
    model = setup(harness)
    assert embeddings.reembed(harness.ctx).created == 0 and not model.calls
    assert not embeddings.preview(harness.ctx).pending_paper_ids


def test_inputs_normalize_unicode_and_only_use_selected_current_fields(harness):
    setup(harness)
    a = add(harness, "Ｍｏｄｅｌ  Ａ")
    evidence = {"text": "  solve\n alpha  ", "quote": "Original", "page": 0, "verified": False}
    saved = extract(
        harness,
        a,
        core=None,
        data={"problem": evidence, "approach": {**evidence, "text": "Use β"}, "datasets": []},
    )
    source = _input(harness, a)
    assert source.input_text == "title: Model A\nproblem: solve alpha\napproach: Use β"
    assert source.source_extraction_id == saved.id
    embeddings.reembed(harness.ctx)
    # 这部分用于候选检索，不把模型概括当成已核实的语义关系。
    assert harness.ctx.store.embeddings.for_paper(a)[0].source_extraction_id == saved.id


@pytest.mark.parametrize("change", ["input", "producer", "document", "missing_document"])
def test_stale_extractions_fall_back_to_metadata_without_reextracting(harness, change):
    setup(harness)
    a = add(harness, "a")
    evidence = {"text": "Derived approach", "quote": "Original", "page": 0}
    saved = extract(
        harness,
        a,
        core=None,
        data={"problem": evidence, "approach": evidence},
        **(
            {"input_hash": "old"}
            if change == "input"
            else {"producer": "old"}
            if change == "producer"
            else {}
        ),
    )
    if change in {"document", "missing_document"}:
        state = harness.ctx.store.papers.get_fulltext(a)
        harness.ctx.store.papers.save_fulltext(
            replace(state, doc_key="blob://new" if change == "document" else None),
            harness.ctx.now_iso(),
        )
    source = _input(harness, a)
    assert source.input_text == "title: a" and source.source_extraction_id is None
    assert source.warnings and "Derived" not in source.input_text
    embeddings.reembed(harness.ctx)
    assert harness.ctx.store.papers.get_extraction(a).id == saved.id
    assert harness.llm_calls() == 0


def test_missing_extract_route_retains_usable_inputs_with_unchecked_diagnostic(harness):
    setup(harness)
    a = add(harness, "a")
    extract(harness, a, core=None, data={"problem": {"text": "Existing problem"}})
    no_extract_route(harness)
    source = _input(harness, a)
    assert source.input_text.endswith("problem: Existing problem")
    assert source.warnings[0].code == "extraction_producer_unchecked"


@pytest.mark.parametrize(
    "data", [[], {"problem": []}, {"problem": {"text": 12}, "approach": "old text"}]
)
def test_legacy_or_malformed_extraction_fields_only_supply_metadata(harness, data):
    setup(harness)
    a = add(harness, "a")
    extract(harness, a, core=None, data=data)
    source = _input(harness, a)
    assert source.input_text == "title: a" and source.source_extraction_id is None
    assert embeddings.reembed(harness.ctx).created == 1


def test_only_actual_text_changes_input_identity_and_history_is_preserved(harness):
    model = setup(harness)
    a = add(harness, "a")
    embeddings.reembed(harness.ctx)
    first = harness.ctx.store.embeddings.for_paper(a)[0]
    harness.ctx.store._db.write(
        update(paper).where(paper.c.id == a).values(abstract="New abstract")
    )
    assert embeddings.preview(harness.ctx).pending_paper_ids == (a,)
    embeddings.reembed(harness.ctx)
    history = harness.ctx.store.embeddings.for_paper(a)
    assert len(history) == 2 and first in history
    assert len(model.calls) == 2
    model.model = replace(model.model, producer_version="producer-v2")
    embeddings.reembed(harness.ctx)
    assert len(harness.ctx.store.embeddings.for_paper(a)) == 3


def test_truncation_is_continuous_and_changes_after_cutoff_do_not_recompute(harness):
    model = setup(harness)
    harness.ctx = replace(
        harness.ctx,
        settings=replace(
            harness.ctx.settings,
            embedding=harness.ctx.settings.embedding.model_copy(update={"max_input_chars": 100}),
        ),
    )
    a = add(harness, "A" * 120)
    source = _input(harness, a)
    assert source.input_text == ("title: " + "A" * 120)[:100]
    embeddings.reembed(harness.ctx)
    harness.ctx.store._db.write(
        update(paper).where(paper.c.id == a).values(abstract="Outside prefix")
    )
    assert embeddings.reembed(harness.ctx).reused == 1 and len(model.calls) == 1


@pytest.mark.parametrize("title_length,used", [(120, False), (83, False), (82, True)])
def test_extraction_provenance_requires_an_actual_value_character_in_prefix(
    harness, title_length, used
):
    setup(harness)
    harness.ctx = replace(
        harness.ctx,
        settings=replace(
            harness.ctx.settings,
            embedding=harness.ctx.settings.embedding.model_copy(update={"max_input_chars": 100}),
        ),
    )
    a = add(harness, "A" * title_length)
    saved = extract(harness, a, core=None, data={"problem": {"text": "Unused problem"}})
    source = _input(harness, a)
    assert len(source.input_text) == 100
    assert (source.source_extraction_id == saved.id) is used
    if title_length == 83:
        assert source.input_text.endswith("problem: ") and source.source_extraction_id is None
    if used:
        assert source.input_text.endswith("problem: U")


def test_unused_extraction_fields_do_not_invalidate_existing_input(harness):
    model = setup(harness)
    a = add(harness, "a")
    extract(harness, a, core=None, data={"datasets": []})
    embeddings.reembed(harness.ctx)
    first = _input(harness, a)
    extract(harness, a, core=None, producer="different", data={"datasets": [{"text": "unused"}]})
    assert _input(harness, a).input_hash == first.input_hash
    assert embeddings.reembed(harness.ctx).reused == 1 and len(model.calls) == 1


def test_model_preparation_only_happens_on_explicit_execution(harness):
    model = setup(harness)
    a = add(harness, "a")
    model.model = replace(
        model.model, producer_version=None, available=False, reason="No local assets"
    )
    model.prepare = True
    assert not embeddings.preview(harness.ctx).model.available and not model.calls
    assert embeddings.similar(harness.ctx, a).matches == () and not model.calls
    assert embeddings.reembed(harness.ctx).created == 1
    assert model.model.available and len(model.calls) == 1


@pytest.mark.parametrize("change", ["input", "model"])
def test_changed_inputs_or_model_during_inference_are_not_saved(harness, change):
    model = setup(harness)
    a = add(harness, "a")
    if change == "input":
        model.hook = lambda _: harness.ctx.store._db.write(
            update(paper).where(paper.c.id == a).values(title="Changed")
        )
    else:
        model.hook = lambda _: setattr(model, "model", replace(model.model, producer_version="new"))
    result = embeddings.reembed(harness.ctx)
    assert result.created == 0 and result.changed_paper_ids == (a,)
    assert not harness.ctx.store.embeddings.for_paper(a)


def test_batch_failure_keeps_prior_batch_and_retry_only_computes_pending(harness):
    model = setup(harness, batch_size=1)
    a, b = add(harness, "a"), add(harness, "b")

    def fail_second(_):
        if len(model.calls) == 2:
            raise ConfigError("simulated local inference failure")

    model.hook = fail_second
    with pytest.raises(ConfigError, match="inference failure"):
        embeddings.reembed(harness.ctx)
    assert sum(len(harness.ctx.store.embeddings.for_paper(key)) for key in (a, b)) == 1
    model.hook = None
    result = embeddings.reembed(harness.ctx)
    assert result.created == 1 and result.reused == 1 and len(model.calls) == 3


@pytest.mark.parametrize("vector", [(0, 0, 0), (float("nan"), 0, 0), (1, 0), (1e100, 0, 0)])
def test_bad_output_rejects_entire_batch_before_any_write(harness, vector):
    model = setup(harness)
    a, b = add(harness, "a"), add(harness, "b")
    model.vectors[_input(harness, b).input_text] = vector
    with pytest.raises(ConfigError):
        embeddings.reembed(harness.ctx)
    assert not harness.ctx.store.embeddings.for_paper(a)
    assert not harness.ctx.store.embeddings.for_paper(b)


@pytest.mark.parametrize("error", ["count", "dimensions", "model", "configuration", "producer"])
def test_inconsistent_batch_metadata_cannot_write_any_rows(harness, monkeypatch, error):
    model = setup(harness)
    a = add(harness, "a")
    changed = {
        "dimensions": {"dimensions": 4},
        "model": {"model": "other"},
        "configuration": {"configuration_version": "other"},
        "producer": {"producer_version": None},
    }
    result = EmbeddingBatch(
        replace(model.model, **changed.get(error, {})), () if error == "count" else ((1, 0, 0),)
    )
    monkeypatch.setattr(model, "embed", lambda _: result)
    with pytest.raises(ConfigError):
        embeddings.reembed(harness.ctx)
    assert not harness.ctx.store.embeddings.for_paper(a)


def test_similar_scores_are_current_local_stable_and_exclude_the_query(harness):
    model = setup(harness)
    a = add(harness, "a")
    others = [add(harness, name) for name in ("b", "c", "d")]
    for key, vector in zip(others, ((1, 1, 0), (-1, 0, 0), (1, 1, 0)), strict=True):
        model.vectors[_input(harness, key).input_text] = vector
    embeddings.reembed(harness.ctx)
    result = embeddings.similar(harness.ctx, a, limit=2)
    assert result.total == 3 and len(result.matches) == 2
    assert [match.paper_id for match in result.matches] == sorted((others[0], others[2]))
    assert all(match.cosine == pytest.approx(2**-0.5) for match in result.matches)
    assert len(model.calls) == 1
    model.model = replace(model.model, producer_version="new")
    assert embeddings.similar(harness.ctx, a).matches == ()
    assert len(model.calls) == 1


def test_disabled_and_unknown_scope_behaviors_are_explicit(harness):
    setup(harness)
    a = add(harness, "a")
    outside = add(harness, "outside", library=False)
    harness.ctx = replace(harness.ctx, embedder=None)
    assert embeddings.preview(harness.ctx).model is None
    assert embeddings.similar(harness.ctx, a).warnings[0].code == "embedding_unavailable"
    with pytest.raises(ConfigError, match="关闭"):
        embeddings.reembed(harness.ctx)
    for key in (outside, "missing"):
        with pytest.raises(NotFound):
            embeddings.similar(harness.ctx, key)
    with pytest.raises(InvalidRequest):
        embeddings.similar(harness.ctx, a, limit=0)
