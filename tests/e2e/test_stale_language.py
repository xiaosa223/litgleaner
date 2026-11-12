"""语言派生物的全历史只读核验，外部模型只用于构造离线历史事实。"""

from dataclasses import asdict, replace

import pytest

from litgleaner.application import qa, translation
from litgleaner.application.documents import load_document, save_document
from litgleaner.application.llm_gateway import Prompt
from litgleaner.application.pipeline import run_on_demand
from litgleaner.application.stages.discover import resolve_record
from litgleaner.application.stale_inputs import StaleInputs
from litgleaner.application.stale_language import check_answers, check_translations
from litgleaner.domain.document import Block, NormalizedDocument
from litgleaner.domain.glossary import Glossary, GlossaryTerm
from litgleaner.domain.hashing import stable_hash
from litgleaner.domain.paper import SourceRecord
from litgleaner.ports.store import FullTextState
from tests.e2e.conftest import Harness
from tests.e2e.stale_language_helpers import legacy_question


def _paper(harness: Harness, title: str = "Alpha evidence") -> str:
    paper_id = resolve_record(
        harness.ctx.store.papers, SourceRecord(title=title), harness.ctx.now_iso()
    )
    document = NormalizedDocument(
        1, stable_hash(title), "test", title,
        (Block("heading", "Abstract", 0),
         Block("text", f"We study alpha evidence in {title}.", 0),
         Block("text", "我们研究模型的学习能力。", 1)), (), "ok",
    )  # fmt: skip
    doc_key, _ = save_document(harness.ctx, document)
    harness.ctx.store.papers.save_fulltext(
        FullTextState(paper_id, "parsed", doc_key=doc_key), harness.ctx.now_iso()
    )
    return paper_id


def _cache(harness: Harness, paper_id: str):
    paragraph = translation.paper_paragraphs(harness.ctx, paper_id)[0]
    key = translation.cache_key(
        harness.ctx, paragraph, translation.paper_glossary(harness.ctx, paper_id)
    )
    harness.ctx.store.translations.put(
        key,
        tuple("研究证据。" for _ in paragraph.sentences),
        "cheap",
        paper_id,
        harness.ctx.now_iso(),
    )
    return key


@pytest.fixture
def saved(harness: Harness):
    harness.edit_yaml(
        "llm.yaml",
        lambda data: data["tasks"].update(
            ask={"provider": "main", "model": "strong", "prompt": "ask"}
        ),
    )
    harness.rebuild()
    paper_id = _paper(harness)
    key = _cache(harness, paper_id)
    harness.llm.overrides["ask"] = {"statements": [{"text": "An inference.", "citations": []}]}
    question = legacy_question(harness, paper_id)
    assert run_on_demand(harness.ctx).ok
    return paper_id, key, question


def _basis(harness: Harness) -> StaleInputs:
    return StaleInputs(harness.ctx, harness.ctx.store.stale_snapshot())


def test_current_translation_and_answer_use_frozen_inputs_without_io_writes(
    harness: Harness, saved, monkeypatch
):
    paper_id, key, _ = saved
    basis = _basis(harness)
    calls, cost = harness.llm_calls(), harness.ctx.store.runs.llm_cost_for_paper(paper_id)
    reads: list[str] = []
    get = harness.ctx.blobs.get

    def read(key):
        reads.append(key)
        return get(key)

    def forbidden(*_args, **_kwargs):
        pytest.fail("stale 检查不得查询数据库、写入或调用模型")

    with monkeypatch.context() as guard:
        db = harness.ctx.store._db
        for name in ("fetch", "write", "atomic"):
            guard.setattr(db, name, forbidden)
        for name in ("complete", "stream"):
            guard.setattr(harness.llm, name, forbidden)
        guard.setattr(harness.ctx.blobs, "put", forbidden)
        guard.setattr(harness.ctx.blobs, "get", read)
        translated = check_translations(basis)
        answered = check_answers(basis)
    assert len(reads) == 1  # 文档和切块按同一归档键复用。
    assert translated[0].id == stable_hash(asdict(key))
    assert translated[0].status == "current" and translated[0].selected
    assert answered[0].status == "unknown" and not answered[0].reasons
    assert answered[0].selected and "完整模型" in " ".join(answered[0].unchecked)
    assert harness.llm_calls() == calls
    assert harness.ctx.store.runs.llm_cost_for_paper(paper_id) == cost


@pytest.mark.parametrize(
    "change",
    [
        "language",
        "glossary",
        "glossary_without_model",
        "prompt",
        "provider",
        "unit",
        "sentences",
        "source",
    ],
)
def test_translation_checks_every_saved_fingerprint_component(harness: Harness, saved, change):
    paper_id, _, _ = saved
    snapshot = harness.ctx.store.stale_snapshot()
    row = snapshot.translations[0]
    if change == "language":
        harness.ctx = replace(
            harness.ctx, settings=replace(harness.ctx.settings, reading_language="en")
        )
    elif change in {"glossary", "glossary_without_model"}:
        glossary = Glossary(terms=(GlossaryTerm(forms=("alpha",), translation="阿尔法"),))
        harness.ctx = replace(harness.ctx, glossaries={"alpha-topic": glossary})
        if change == "glossary_without_model":
            harness.ctx = replace(
                harness.ctx,
                llm=replace(
                    harness.ctx.llm,
                    routes={k: v for k, v in harness.ctx.llm.routes.items() if k != "translate"},
                ),
            )
    elif change in {"prompt", "provider"}:
        route = harness.ctx.llm.routes["translate"]
        changed = (
            replace(route, prompt=Prompt("translate", route.prompt.text + "\nNew rule"))
            if change == "prompt"
            else replace(route, provider_version="new-provider")
        )
        harness.ctx = replace(
            harness.ctx,
            llm=replace(
                harness.ctx.llm,
                routes={
                    **harness.ctx.llm.routes,
                    "translate": changed,
                },
            ),
        )
    elif change == "source":
        state = snapshot.fulltexts[paper_id]
        doc = load_document(harness.ctx, state.doc_key)
        save_document(harness.ctx, replace(doc, blocks=(Block("text", "Different evidence.", 0),)))
    else:
        cache = (
            replace(row.cache, key=replace(row.cache.key, unit="sentence"))
            if change == "unit"
            else replace(row.cache, sentences=())
        )
        snapshot = replace(snapshot, translations=(replace(row, cache=cache),))
    result = check_translations(StaleInputs(harness.ctx, snapshot))[0]
    assert result.status == "stale" and result.reasons
    assert harness.llm_calls("translate") == 0


def test_translation_exact_cache_wins_then_latest_compatible_fallback(harness: Harness, saved):
    paper_id, key, _ = saved
    harness.ctx.store.translations.put(
        replace(key, translator_version="older-version"),
        ("较新的旧译文。",),
        "legacy",
        paper_id,
        "2026-11-04T00:00:00+00:00",
    )
    selected = [entry for entry in check_translations(_basis(harness)) if entry.selected]
    assert [entry.id for entry in selected] == [stable_hash(asdict(key))]
    harness.ctx = replace(
        harness.ctx,
        llm=replace(
            harness.ctx.llm,
            routes={k: v for k, v in harness.ctx.llm.routes.items() if k != "translate"},
        ),
    )
    results = check_translations(_basis(harness))
    chosen = next(entry for entry in results if entry.selected)
    assert chosen.id == stable_hash(asdict(replace(key, translator_version="older-version")))
    assert chosen.status == "unknown" and not chosen.reasons
    assert "生产者版本" in " ".join(chosen.unchecked)


def test_global_translation_keeps_no_owner_and_is_unknown_only_when_unlocatable(
    harness: Harness, saved
):
    snapshot = harness.ctx.store.stale_snapshot()
    row = snapshot.translations[0]
    matching = replace(row, paper_id=None)
    missing = replace(
        matching,
        cache=replace(
            row.cache,
            key=replace(
                row.cache.key,
                source_hash=stable_hash("An unavailable paragraph."),
            ),
        ),
    )
    snapshot = replace(snapshot, translations=(matching, missing))
    results = check_translations(StaleInputs(harness.ctx, snapshot))
    assert results[0].status == "current" and results[0].selected
    assert results[0].paper_id is None
    assert results[1].status == "unknown" and not results[1].reasons
    assert results[1].paper_id is None and results[1].selected is None


@pytest.mark.parametrize(
    "change", ["language", "prompt", "system_budget", "profile", "selection", "document", "chunker"]
)
def test_answer_checks_prompt_language_and_rebuilds_actual_inputs(harness: Harness, saved, change):
    paper_id, _, question = saved
    snapshot = harness.ctx.store.stale_snapshot()
    if change == "language":
        harness.ctx = replace(
            harness.ctx, settings=replace(harness.ctx.settings, reading_language="en")
        )
    elif change in {"prompt", "system_budget"}:
        route = harness.ctx.llm.routes["ask"]
        text = route.prompt.text + "\nChanged" if change == "prompt" else "x" * 12000
        harness.ctx = replace(
            harness.ctx,
            llm=replace(
                harness.ctx.llm,
                routes={
                    **harness.ctx.llm.routes,
                    "ask": replace(route, prompt=Prompt("ask", text)),
                },
            ),
        )
    elif change == "profile":
        topic = harness.ctx.topics["alpha-topic"]
        harness.ctx = replace(
            harness.ctx,
            topics={
                topic.id: topic.model_copy(update={"background": "A new research background"}),
            },
        )
    elif change == "selection":
        snapshot = replace(
            snapshot, questions=(replace(question, selection="Absent text", page=0),)
        )
    elif change == "document":
        doc = load_document(harness.ctx, snapshot.fulltexts[paper_id].doc_key)
        save_document(harness.ctx, replace(doc, blocks=(Block("text", "Changed content.", 0),)))
    else:
        harness.ctx = replace(
            harness.ctx,
            settings=replace(
                harness.ctx.settings,
                chunker=replace(harness.ctx.settings.chunker, target_tokens=100),
            ),
        )
    result = check_answers(StaleInputs(harness.ctx, snapshot))[0]
    assert result.status == "stale" and result.reasons
    if change == "system_budget":
        assert "当前选区或问答输入已不再有效" in result.reasons
    assert harness.llm_calls("ask") == 1


def test_answer_response_model_does_not_imply_current_configured_model(harness: Harness, saved):
    snapshot = harness.ctx.store.stale_snapshot()
    snapshot = replace(
        snapshot, answers=(replace(snapshot.answers[0], model="response-model-alias"),)
    )
    route = harness.ctx.llm.routes["ask"]
    harness.ctx = replace(
        harness.ctx,
        llm=replace(
            harness.ctx.llm,
            routes={
                **harness.ctx.llm.routes,
                "ask": replace(route, model="new-model", provider_version="other"),
            },
        ),
    )
    result = check_answers(StaleInputs(harness.ctx, snapshot))[0]
    assert result.status == "unknown" and not result.reasons
    assert result.unchecked


def test_without_ask_model_saved_language_document_and_chunker_are_still_checked(
    harness: Harness, saved
):
    paper_id, _, _ = saved
    snapshot = harness.ctx.store.stale_snapshot()
    doc = load_document(harness.ctx, snapshot.fulltexts[paper_id].doc_key)
    save_document(harness.ctx, replace(doc, title="Changed document hash"))
    harness.ctx = replace(
        harness.ctx,
        llm=replace(harness.ctx.llm, routes={}),
        settings=replace(
            harness.ctx.settings,
            reading_language="en",
            chunker=replace(harness.ctx.settings.chunker, target_tokens=100),
        ),
    )
    result = check_answers(StaleInputs(harness.ctx, snapshot))[0]
    assert {"阅读语言已变化", "原文内容已变化", "切分规则已变化"}.issubset(result.reasons)
    assert "未配置" in " ".join(result.unchecked)
    assert harness.llm_calls("ask") == 1


def test_every_historical_answer_is_included_beyond_reader_history_limit(harness: Harness, saved):
    paper_id, key, _ = saved
    original = harness.ctx.store.stale_snapshot().answers[0]
    for number in range(60):
        question = qa.queue(
            harness.ctx, paper_id, "Explain alpha evidence", request_id=f"old-{number}"
        )
        assert harness.ctx.store.qa.add_answer(
            replace(
                original,
                id=f"answer-{number}",
                question_id=question.id,
            )
        )
        harness.ctx.store.translations.put(
            replace(key, translator_version=f"historic-{number}"),
            ("旧译文。",),
            "legacy",
            paper_id,
            harness.ctx.now_iso(),
        )
    qa.queue(harness.ctx, paper_id, "A pending question", request_id="unanswered")
    assert len(qa.history(harness.ctx, paper_id)) == 50
    results = check_answers(_basis(harness))
    assert len(results) == 61 and len({entry.id for entry in results}) == 61
    assert all(entry.status == "unknown" and not entry.reasons for entry in results)
    translations = check_translations(_basis(harness))
    assert len(translations) == 61 and sum(bool(row.selected) for row in translations) == 1
    assert harness.llm_calls("ask") == 1


def test_bad_document_only_affects_its_own_translation_and_answer(harness: Harness, saved):
    paper_id, _, _ = saved
    other = _paper(harness, "Another alpha study")
    _cache(harness, other)
    question = qa.queue(harness.ctx, other, "Explain alpha evidence", request_id="other")
    assert run_on_demand(harness.ctx).ok
    snapshot = harness.ctx.store.stale_snapshot()
    harness.ctx.blobs.put(snapshot.fulltexts[paper_id].doc_key, b"not a document archive")
    basis = StaleInputs(harness.ctx, snapshot)
    results = (*check_translations(basis), *check_answers(basis))
    bad = [entry for entry in results if entry.paper_id == paper_id]
    good = [entry for entry in results if entry.paper_id == other]
    assert len(bad) == len(good) == 2
    assert all(entry.status == "unknown" and "损坏" in " ".join(entry.unchecked) for entry in bad)
    assert next(entry for entry in good if entry.kind == "translation").status == "current"
    assert (
        next(entry for entry in good if entry.kind == "answer").id
        == qa.answer(harness.ctx, question.id).id
    )


@pytest.mark.parametrize("missing", ["questions", "ask_jobs", "fulltexts"])
def test_missing_historical_evidence_is_unknown_instead_of_verified(
    harness: Harness, saved, missing
):
    snapshot = harness.ctx.store.stale_snapshot()
    value = () if missing == "questions" else {}
    result = check_answers(StaleInputs(harness.ctx, replace(snapshot, **{missing: value})))[0]
    assert result.status == "unknown" and not result.reasons
    assert result.unchecked and harness.llm_calls("ask") == 1
