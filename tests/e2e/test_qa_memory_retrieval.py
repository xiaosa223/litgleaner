"""问答记忆只读召回真实规范源；本地向量不得准备资产或写入。"""

from collections import Counter
from dataclasses import asdict, replace

import pytest

from litgleaner.application import embeddings, relation_feedback
from litgleaner.application.documents import save_document
from litgleaner.application.qa_memory_candidates import ScanBudget, document_candidates
from litgleaner.application.qa_memory_retrieval import collect
from litgleaner.application.stages.extract import extraction_input_hash, extraction_producer_version
from litgleaner.application.stale_inputs import StaleInputs
from litgleaner.domain.document import Block, NormalizedDocument
from litgleaner.domain.embedding import EmbeddingBatch, EmbeddingModel
from litgleaner.domain.errors import ConfigError
from litgleaner.domain.hashing import stable_hash
from litgleaner.domain.qa import Answer, Question, Statement
from litgleaner.domain.reading import NewAnnotation
from litgleaner.ports.store import FullTextState
from tests.e2e.memory_helpers import add
from tests.e2e.relate_flow_helpers import change_prompt, generate, respond
from tests.e2e.relate_helpers import pair

MAIN = "The current treatment improves survival substantially."
OTHER = "A distinct mechanism offers an alternative experimental explanation."


def _paper(harness, name, text=MAIN, *, library=True, indexed=True, blocks=None):
    paper_id = add(harness, name, library=library)
    doc = NormalizedDocument(
        1, stable_hash(name), "memory-retrieval", name,
        tuple(blocks or [Block("heading", "Abstract", 0), Block("text", text, 1)]), (), "ok",
    )  # fmt: skip
    key, _ = save_document(harness.ctx, doc)
    harness.ctx.store.papers.save_fulltext(
        FullTextState(paper_id, "parsed", doc_key=key), harness.ctx.now_iso()
    )
    if indexed:
        harness.ctx.store.search.replace_document(paper_id, key, doc.blocks)
    return paper_id


def _question(harness, paper_id, text="Explain treatment survival", **changes):
    return Question(
        "question-new",
        paper_id,
        "request-new",
        text,
        changes.get("selection"),
        changes.get("page"),
        "zh-CN",
        harness.ctx.now_iso(),
    )


def _collect(harness, question):
    return collect(
        harness.ctx, question, StaleInputs(harness.ctx, harness.ctx.store.stale_snapshot())
    )


class ReadyEmbedder:
    def __init__(self):
        self.model = EmbeddingModel("retrieval-local", 3, "config", "producer", True)
        self.calls = []
        self.hook = None
        self.invalid = None
        self.values = {}

    def describe(self):
        return self.model

    def embed(self, texts, *, allow_download=True):
        self.calls.append((texts, allow_download))
        model = self.model
        if self.hook:
            self.hook()
        if self.invalid == "raise":
            raise ConfigError("private model path")
        vectors = tuple(self.values.get(text.strip(), (1.0, 0.0, 0.0)) for text in texts)
        if self.invalid == "zero":
            vectors = tuple((0.0, 0.0, 0.0) for _ in texts)
        elif self.invalid == "nan":
            vectors = tuple((float("nan"), 0.0, 0.0) for _ in texts)
        elif self.invalid == "count":
            vectors = ()
        elif self.invalid == "dimension":
            vectors = tuple((1.0, 0.0) for _ in texts)
        return EmbeddingBatch(model, vectors)


def test_own_lexical_evidence_survives_without_library_membership_fts_or_vectors(harness):
    harness.ctx = replace(harness.ctx, embedder=None)
    paper_id = _paper(harness, "not-library", library=False, indexed=False)
    result = _collect(harness, _question(harness, paper_id))
    assert any(s.paper_id == paper_id and s.text == MAIN and s.page == 1 for s in result.sources)
    assert all(s.kind == "author" for s in result.sources)
    assert result.warnings and result.relations == ()


def test_fts_reads_canonical_author_user_notes_and_questions_but_not_model_answers(harness):
    harness.ctx = replace(harness.ctx, embedder=None)
    main = _paper(harness, "main")
    other = _paper(harness, "other", OTHER)
    outsider = _paper(harness, "outside", OTHER, library=False)
    note = harness.ctx.store.reader.add_annotation(
        NewAnnotation(other, "note", None, None, None, "My survival treatment observation."),
        harness.ctx.now_iso(),
    )
    previous = replace(_question(harness, other), id="previous", request_id="previous")
    harness.ctx.store.qa.add_question(previous)
    harness.ctx.store.qa.add_answer(
        Answer(
            "answer",
            other,
            previous.id,
            (Statement("Model survival treatment", (), True),),
            "fake",
            "prompt",
            "input",
            harness.ctx.now_iso(),
        )
    )
    result = _collect(harness, _question(harness, main))
    assert outsider not in {s.paper_id for s in result.sources}
    notes = [s for s in result.sources if s.kind == "note"]
    assert len(notes) == 1 and notes[0].source_id == note.id and notes[0].page is None
    assert notes[0].version == stable_hash(asdict(note))
    questions = [s for s in result.sources if s.kind == "question"]
    assert len(questions) == 1 and questions[0].source_id == previous.id
    assert questions[0].version == stable_hash(asdict(previous))
    assert "Model survival treatment" not in {s.text for s in result.sources}


def test_fts_author_hit_checks_actual_block_even_when_pointer_is_unchanged(harness):
    harness.ctx = replace(harness.ctx, embedder=None)
    main = _paper(harness, "main")
    other = _paper(harness, "other", "The treatment survival evidence belongs to this work.")
    state = harness.ctx.store.papers.get_fulltext(other)
    old = NormalizedDocument(
        1, stable_hash("other"), "memory-retrieval", "other", (Block("text", OTHER, 1),), (), "ok"
    )
    key, _ = save_document(harness.ctx, old)
    assert key == state.doc_key
    result = _collect(harness, _question(harness, main))
    assert other not in {s.paper_id for s in result.sources}


def test_ready_vectors_recall_unindexed_unmatched_library_paper_without_download(harness):
    main = _paper(harness, "main")
    other = _paper(harness, "other", OTHER, indexed=False)
    model = ReadyEmbedder()
    harness.ctx = replace(harness.ctx, embedder=model)
    embeddings.reembed(harness.ctx)
    model.calls.clear()
    result = _collect(harness, _question(harness, main))
    assert other in {s.paper_id for s in result.sources}
    assert any(s.paper_id == other and s.text == OTHER for s in result.sources)
    assert model.calls and all(download is False for _, download in model.calls)
    assert all(len(texts) <= 64 for texts, _ in model.calls)
    assert not result.warnings


@pytest.mark.parametrize("invalid", ["raise", "zero", "nan", "count", "dimension"])
def test_invalid_local_vectors_degrade_without_losing_own_or_note_sources(harness, invalid):
    paper_id = _paper(harness, "main")
    note = harness.ctx.store.reader.add_annotation(
        NewAnnotation(paper_id, "note", None, None, None, "My treatment survival note."),
        harness.ctx.now_iso(),
    )
    model = ReadyEmbedder()
    model.invalid = invalid
    harness.ctx = replace(harness.ctx, embedder=model)
    result = _collect(harness, _question(harness, paper_id))
    assert any(s.text == MAIN for s in result.sources)
    assert any(s.source_id == note.id for s in result.sources)
    assert result.warnings and "private" not in " ".join(result.warnings)
    assert all(download is False for _, download in model.calls)


def test_model_identity_changed_during_query_embedding_discards_vector_signal(harness):
    paper_id = _paper(harness, "main")
    model = ReadyEmbedder()
    model.hook = lambda: setattr(model, "model", replace(model.model, producer_version="changed"))
    harness.ctx = replace(harness.ctx, embedder=model)
    result = _collect(harness, _question(harness, paper_id))
    assert result.warnings and any(s.text == MAIN for s in result.sources)


def test_unavailable_embedder_is_never_called(harness):
    paper_id = _paper(harness, "main")
    model = ReadyEmbedder()
    model.model = replace(model.model, available=False)
    harness.ctx = replace(harness.ctx, embedder=model)
    assert _collect(harness, _question(harness, paper_id)).sources
    assert not model.calls


def test_retrieval_reads_each_document_once_and_never_writes_or_calls_external_services(
    harness, monkeypatch
):
    harness.ctx = replace(harness.ctx, embedder=None)
    paper_id = _paper(harness, "main")
    _paper(harness, "other", "A treatment improves survival through another mechanism.")
    basis = StaleInputs(harness.ctx, harness.ctx.store.stale_snapshot())
    reads, queries = [], []
    get, fetch = harness.ctx.blobs.get, harness.ctx.store._db.fetch

    def read(key):
        reads.append(key)
        return get(key)

    def query(statement):
        queries.append(statement)
        return fetch(statement)

    def forbidden(*_args, **_kwargs):
        pytest.fail("retrieval must not write, call models, or use external services")

    monkeypatch.setattr(harness.ctx.blobs, "get", read)
    monkeypatch.setattr(harness.ctx.blobs, "put", forbidden)
    monkeypatch.setattr(harness.ctx.store._db, "fetch", query)
    monkeypatch.setattr(harness.ctx.store._db, "write", forbidden)
    monkeypatch.setattr(type(harness.ctx.llm), "call", forbidden)
    for source in harness.ctx.sources.values():
        monkeypatch.setattr(type(source), "fetch", forbidden)
    assert collect(harness.ctx, _question(harness, paper_id), basis).sources
    assert len(queries) == 1 and max(Counter(reads).values()) == 1


def test_shared_scan_budget_cannot_expand_per_document_or_long_source(harness):
    harness.ctx = replace(harness.ctx, embedder=None)
    a = _paper(harness, "main", "treatment " * 300000, indexed=False)
    b = _paper(harness, "other", "survival " * 300000, indexed=False)
    basis = StaleInputs(harness.ctx, harness.ctx.store.stale_snapshot())
    budget = ScanBudget()
    first = document_candidates(basis, _question(harness, a), a, budget)
    second = document_candidates(basis, _question(harness, a), b, budget)
    assert budget.chars >= 0 and budget.sources >= 0
    assert len(first) + len(second) <= 4000
    assert all(len(row.source.text.encode("utf-8")) <= 900 for row in (*first, *second))


def test_valid_relation_keeps_both_frozen_original_sides_and_reject_removes_it(
    harness, monkeypatch
):
    harness.ctx = replace(harness.ctx, embedder=None)
    a, b = pair(harness)
    respond(harness, monkeypatch)
    generate(harness, a)
    relation = harness.ctx.store.relations.for_paper(a)[0]
    result = _collect(harness, _question(harness, a))
    assert result.relations == (relation,)
    sides = {s.id: s for s in result.sources if s.id.startswith("relation:")}
    for evidence in (relation.evidence_a, relation.evidence_b):
        source = sides[f"relation:{relation.id}:{evidence.paper_id}"]
        assert (source.text, source.page, source.doc_key) == (
            evidence.quote,
            evidence.page,
            evidence.doc_key,
        )
    assert {s.paper_id for s in sides.values()} == {a, b}
    relation_feedback.apply(harness.ctx, relation.id, "reject", "reject-for-qa")
    assert _collect(harness, _question(harness, a)).relations == ()


@pytest.mark.parametrize("change", ["prompt", "missing-archive"])
def test_stale_or_unknown_relation_never_enters_qa(harness, monkeypatch, change):
    harness.ctx = replace(harness.ctx, embedder=None)
    a, b = pair(harness)
    respond(harness, monkeypatch)
    generate(harness, a)
    if change == "prompt":
        change_prompt(harness)
    else:
        key = harness.ctx.store.papers.get_fulltext(b).doc_key
        original = harness.ctx.blobs.get

        def missing(pointer):
            if pointer == key:
                raise ValueError("private archive path")
            return original(pointer)

        monkeypatch.setattr(harness.ctx.blobs, "get", missing)
    result = _collect(harness, _question(harness, a))
    assert not result.relations
    assert not any(s.id.startswith("relation:") for s in result.sources)
    assert "private" not in " ".join(result.warnings)


def test_background_is_current_model_data_with_complete_extraction_fingerprint(harness):
    harness.ctx = replace(harness.ctx, embedder=None)
    paper_id = _paper(harness, "main")
    ctx = harness.ctx
    key = ctx.store.papers.get_fulltext(paper_id).doc_key
    input_hash = extraction_input_hash(
        key, ctx.settings.chunker, ctx.settings.reading_language, ctx.settings.assess_token_budget
    )
    producer = extraction_producer_version(ctx)
    data = {"summary": "A model background. " * 30, "problem": {"text": "Model problem"}}
    ctx.store.papers.insert_extraction(paper_id, input_hash, producer, data, ctx.now_iso())
    extraction = ctx.store.papers.get_extraction(paper_id)
    result = _collect(harness, _question(harness, paper_id))
    background = result.backgrounds[paper_id]
    assert background["id"] == extraction.id
    assert background["content_hash"] == stable_hash(asdict(extraction))
    assert background["input_hash"] == input_hash and background["producer_version"] == producer
    assert len(background["fields"]["summary"]) == 200
    assert background["fields"]["problem"] == "Model problem"
    assert not any(s.text == "Model problem" for s in result.sources)
    ctx.store.papers.insert_extraction(paper_id, "old", producer, data, ctx.now_iso())
    assert not _collect(harness, _question(harness, paper_id)).backgrounds


def test_query_cosine_selects_matching_cached_paper_instead_of_arbitrary_neighbors(harness):
    main = _paper(harness, "main")
    target = _paper(harness, "semantic-target", OTHER, indexed=False)
    for index in range(6):
        _paper(harness, f"orthogonal-{index}", OTHER, indexed=False)
    model = ReadyEmbedder()
    model.values = {
        "title: semantic-target": (0.0, 1.0, 0.0),
        "Explain different mechanisms": (0.0, 1.0, 0.0),
    }
    harness.ctx = replace(harness.ctx, embedder=model)
    embeddings.reembed(harness.ctx)
    model.calls.clear()
    result = _collect(harness, _question(harness, main, "Explain different mechanisms"))
    assert {s.paper_id for s in result.sources} == {main, target}
    assert model.calls and all(download is False for _, download in model.calls)


def test_fts_changed_after_snapshot_cannot_supply_different_user_note_as_frozen_source(harness):
    harness.ctx = replace(harness.ctx, embedder=None)
    main = _paper(harness, "main")
    other = _paper(harness, "other", OTHER)
    note = harness.ctx.store.reader.add_annotation(
        NewAnnotation(other, "note", None, None, None, "Original treatment survival note."),
        harness.ctx.now_iso(),
    )
    basis = StaleInputs(harness.ctx, harness.ctx.store.stale_snapshot())
    harness.ctx.store.reader.update_annotation(
        note.id, "Revised treatment survival note.", harness.ctx.now_iso()
    )
    result = collect(harness.ctx, _question(harness, main), basis)
    assert not any(s.source_id == note.id for s in result.sources)
