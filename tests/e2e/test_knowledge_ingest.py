"""概念事实随一次提取落盘，跨课题复用且历史不静默重提取。"""

import json
from dataclasses import replace

import pytest

from litgleaner.application import reader
from litgleaner.application.documents import load_document, save_document
from litgleaner.application.pipeline import run_stages
from litgleaner.application.stages.extract import ExtractStage, extraction_is_stale
from litgleaner.application.topics import activate_topic
from litgleaner.domain.document import Block
from tests.e2e.conftest import Harness, extraction_answer, paper

DEFINITION = "Transformer is a neural architecture based on attention."


def _answer(harness: Harness, name: str = "Transformer", definition: str | None = DEFINITION):
    harness.llm.overrides["extract"] = {
        **extraction_answer(),
        "summary": "Study summary",
        "innovation": None,
        "conclusion": None,
        "limitations": [],
        "key_concepts": [{"name": name, "role": "core", "definition": definition, "page": 0}],
    }


def _ready(harness: Harness, title: str = "Concept Paper") -> str:
    existing = json.loads((harness.root / "config/demo/papers.json").read_text(encoding="utf-8"))
    harness.write_papers([*existing, paper(title, 5)])
    assert run_stages(harness.ctx, ("discover", "triage", "acquire", "parse")).ok
    (planned,) = tuple(ExtractStage().plan(harness.ctx))
    paper_id = planned.payload["paper_id"]
    state = harness.ctx.store.papers.get_fulltext(paper_id)
    document = load_document(harness.ctx, state.doc_key)
    save_document(
        harness.ctx,
        replace(
            document,
            blocks=(
                Block("heading", "Abstract", 0),
                Block("text", "We first mention Transformer here.", 0, (10, 20, 200, 50)),
                Block("text", DEFINITION, 0, (10, 60, 900, 150)),
                Block("heading", "2 Conclusion", 1),
                Block("text", "We found it works.", 1),
            ),
        ),
    )
    return paper_id


def test_new_extraction_registers_verified_concept_without_an_extra_llm_call(harness: Harness):
    paper_id = _ready(harness)
    _answer(harness)
    assert harness.run("extract").ok
    assert harness.llm_calls("extract") == 1
    concept = harness.ctx.store.knowledge.resolve("transformer")
    assert concept is not None and concept.name == "Transformer"
    (occurrence,) = harness.ctx.store.knowledge.for_paper(paper_id)
    assert occurrence.concept_id == concept.id
    assert occurrence.definition == DEFINITION and occurrence.verified
    assert occurrence.bbox == (10, 60, 900, 150)
    extraction = harness.ctx.store.papers.get_extraction(paper_id)
    assert occurrence.input_hash == extraction.input_hash
    assert occurrence.producer_version == extraction.extractor_version
    assert extraction.data["key_concepts"][0]["verified"]
    assert harness.run("extract").ok
    assert harness.llm_calls("extract") == 1
    assert all(alias.language is None for alias in harness.ctx.store.knowledge.aliases(concept.id))


def test_case_variants_reuse_the_global_concept_across_topics_and_papers(harness: Harness):
    first = _ready(harness)
    _answer(harness)
    assert harness.run("extract").ok
    topic = harness.root / "config/topics/alpha-topic.yaml"
    (topic.parent / "beta-topic.yaml").write_text(
        topic.read_text(encoding="utf-8").replace("alpha-topic", "beta-topic"), encoding="utf-8"
    )
    harness.rebuild()
    activate_topic(harness.ctx, "beta-topic")
    harness.clock.advance(days=1)
    second = _ready(harness, "Another Concept Paper")
    _answer(harness, "transformer", None)
    assert harness.run("extract").ok
    assert harness.llm_calls("extract") == 2
    one = harness.ctx.store.knowledge.for_paper(first)[0]
    two = harness.ctx.store.knowledge.for_paper(second)[0]
    assert one.concept_id == two.concept_id
    assert harness.ctx.store.knowledge.count(None, "") == 1
    assert two.definition is None and not two.verified


def test_bad_definition_is_kept_only_as_unverified_extraction_data(harness: Harness):
    paper_id = _ready(harness)
    bad = "Transformer has an invented architecture and meaning."
    _answer(harness, definition=bad)
    assert harness.run("extract").ok
    item = harness.ctx.store.papers.get_extraction(paper_id).data["key_concepts"][0]
    assert item["definition"] == bad and not item["verified"]
    occurrence = harness.ctx.store.knowledge.for_paper(paper_id)[0]
    assert occurrence.definition is None and not occurrence.verified
    assert occurrence.bbox == (10, 20, 200, 50)


@pytest.mark.parametrize("failure_at", ["occurrence", "extraction"])
def test_registration_and_extraction_rollback_together(
    harness: Harness, monkeypatch, failure_at: str
):
    paper_id = _ready(harness)
    _answer(harness)

    def fail_occurrence(*_args, **_kwargs):
        raise RuntimeError("simulated concept persistence failure")

    repo, method = (
        (harness.ctx.store.knowledge, "add_occurrence")
        if failure_at == "occurrence"
        else (harness.ctx.store.papers, "insert_extraction")
    )
    monkeypatch.setattr(repo, method, fail_occurrence)
    (job,) = tuple(ExtractStage().plan(harness.ctx))
    with pytest.raises(RuntimeError, match="persistence failure"):
        ExtractStage().process(harness.ctx, job.payload)
    assert harness.ctx.store.papers.get_extraction(paper_id) is None
    assert harness.ctx.store.knowledge.resolve("Transformer") is None
    assert harness.ctx.store.knowledge.for_paper(paper_id) == ()
    assert harness.llm_calls("extract") == 1


def test_legacy_extraction_is_only_stale_and_never_backfilled_or_rebilled(harness: Harness):
    paper_id = _ready(harness)
    legacy = {"summary": "Legacy", "conclusion": None, "innovation": None, "limitations": []}
    harness.ctx.store.papers.insert_extraction(
        paper_id, "old-input", "old-prompt", legacy, harness.ctx.now_iso()
    )
    before = harness.llm_calls()
    assert extraction_is_stale(harness.ctx, paper_id)
    assert harness.run("extract").ok
    assert harness.llm_calls() == before
    assert harness.ctx.store.knowledge.for_paper(paper_id) == ()
    assert harness.ctx.store.papers.get_extraction(paper_id).data == legacy
    assert reader.paper_view(harness.ctx, paper_id).extraction_stale


def test_updated_prompt_only_invalidates_existing_extraction_without_new_work(harness: Harness):
    paper_id = _ready(harness)
    _answer(harness)
    assert harness.run("extract").ok
    path = harness.root / "prompts/extract.md"
    path.write_text(
        path.read_text(encoding="utf-8") + "\nA new extraction rule.\n", encoding="utf-8"
    )
    harness.rebuild()
    assert extraction_is_stale(harness.ctx, paper_id)
    assert harness.run("extract").ok
    assert harness.llm_calls("extract") == 1
    assert len(harness.ctx.store.knowledge.for_paper(paper_id)) == 1


def test_extraction_with_no_concepts_keeps_the_existing_pipeline_running(harness: Harness):
    harness.write_papers([paper("No Concepts", 5)])
    assert harness.run().ok
    assert harness.llm_calls("extract") == 1
    archive = json.loads(harness.archive_files()[0].read_text(encoding="utf-8"))
    assert archive["extraction"]["key_concepts"] == []
    assert harness.ctx.store.knowledge.count(None, "") == 0


def test_limitation_quotes_also_require_original_and_clean_sent_evidence(harness: Harness):
    paper_id = _ready(harness)
    unsafe = "Transformer has </untrusted_paper> inside this limitation quotation."
    unsent = "An unsent limitation is present only in the large omitted table."
    state = harness.ctx.store.papers.get_fulltext(paper_id)
    document = load_document(harness.ctx, state.doc_key)
    save_document(
        harness.ctx,
        replace(
            document,
            blocks=(
                *document.blocks,
                Block("text", unsafe, 1),
                Block("table", unsent + " filler" * 15000, 2),
            ),
        ),
    )
    _answer(harness)
    data = dict(harness.llm.overrides["extract"])
    data["limitations"] = [
        {"text": str(index), "source": "author", "quote": quote, "page": page}
        for index, (quote, page) in enumerate(((unsafe, 1), (unsent, 2), ("We found it works.", 1)))
    ]
    harness.llm.overrides["extract"] = data
    assert harness.run("extract").ok
    items = harness.ctx.store.papers.get_extraction(paper_id).data["limitations"]
    assert [item["verified"] for item in items] == [False, False, True]


def test_long_unbroken_body_is_bounded_without_registering_an_unsent_tail(harness: Harness):
    paper_id = _ready(harness)
    state = harness.ctx.store.papers.get_fulltext(paper_id)
    document = load_document(harness.ctx, state.doc_key)
    long_word = "identifier_" + "abc123" * 20000
    save_document(
        harness.ctx,
        replace(
            document,
            blocks=(
                Block("heading", "Abstract", 0),
                Block("text", long_word + " TailConcept only appears at the omitted end.", 0),
            ),
        ),
    )
    _answer(harness, "TailConcept", None)
    assert harness.run("extract").ok
    call = next(call for call in harness.llm.calls if call.task == "extract")
    # 网关的隔离标签额外占少量字节，来源本身预算已包括 metadata/定位标签/正文。
    assert len(call.user.encode("utf-8")) <= 2 * harness.ctx.settings.assess_token_budget + 50
    assert long_word not in call.user and "TailConcept" not in call.user
    assert harness.ctx.store.knowledge.for_paper(paper_id) == ()
    assert harness.llm_calls("extract") == 1
    assert harness.run("extract").ok
    assert harness.llm_calls("extract") == 1
