"""完整事实随原有提取调用落盘，旧结果只失效、不自动付费重做。"""

import json
from dataclasses import replace

import pytest

from litgleaner.application import knowledge, reader
from litgleaner.application.documents import load_document, save_document
from litgleaner.application.export import mine
from litgleaner.application.pipeline import run_stages
from litgleaner.application.stages import extract
from litgleaner.domain.document import Block
from litgleaner.domain.errors import PermanentError, TransientError
from litgleaner.domain.export import export_name
from tests.e2e.conftest import Harness, extraction_answer, paper

METHOD = "We introduce a graph method to address distribution shifts."
RESULT = "On DatasetAlpha, accuracy is 93.2% over five runs with the same split."
CODE = "Our code is publicly available at https://code.example/Group/Model."
CLAIM = "Our results show that the graph method improves robustness."
URL = "https://code.example/Group/Model"
BOX = (10, 20, 900, 80)


def _fact(quote: str, page: int, text: str = "模型概括") -> dict:
    return {"text": text, "quote": quote, "page": page}


def _ready(harness: Harness) -> str:
    harness.write_papers([paper("Structured Evidence", 5)])
    assert run_stages(harness.ctx, ("discover", "triage", "acquire", "parse")).ok
    (job,) = tuple(extract.ExtractStage().plan(harness.ctx))
    paper_id = job.payload["paper_id"]
    state = harness.ctx.store.papers.get_fulltext(paper_id)
    document = load_document(harness.ctx, state.doc_key)
    save_document(
        harness.ctx,
        replace(
            document,
            blocks=(
                Block("heading", "Abstract", 0),
                Block("text", METHOD, 0, BOX),
                Block("text", RESULT, 1, (10, 30, 900, 90)),
                Block("text", CODE, 1),
                Block("heading", "Conclusion", 2),
                Block("text", CLAIM, 2),
            ),
        ),
    )
    return paper_id


def _answer() -> dict:
    return extraction_answer(
        summary="演示模型摘要",
        problem=_fact(METHOD, 0),
        prior_gap=_fact("Prior systems cannot solve this invented problem.", 0),
        approach=_fact(METHOD, 0),
        method=_fact(METHOD, 0, "图方法：处理分布变化"),
        claims=[_fact(CLAIM, 2)],
        datasets=[_fact(RESULT, 1, "DatasetAlpha")],
        metrics=[_fact(RESULT, 1, "accuracy")],
        results=[_fact(RESULT, 1, "相同划分、五次实验，准确率 93.2%")],
        code_data=[
            {"kind": "code", "availability": "public", "url": URL, **_fact(CODE, 1)},
            {"kind": "data", "availability": "unknown", "url": URL + "/fake", **_fact(CODE, 1)},
        ],
        limitations=[
            {"source": "author", **_fact(RESULT, 0)},
            {"source": "inferred", **_fact(METHOD, 0)},
        ],
        questions=[
            {"question": "采用什么方法？", "answer": _fact(METHOD, 0)},
            {"question": "未覆盖的条件是什么？", "answer": None},
            {"question": "准确率有什么依据？", "answer": _fact(RESULT, 0)},
        ],
        highlights=[
            {"kind": "method", "quote": METHOD, "page": 0},
            {"kind": "result", "quote": RESULT, "page": 0},
        ],
        key_concepts=[{"name": "graph", "role": "core", "definition": METHOD, "page": 0}],
    )


def _extracted(harness: Harness) -> str:
    paper_id = _ready(harness)
    harness.llm.overrides["extract"] = _answer()
    assert harness.run("extract").ok
    return paper_id


def test_all_facts_are_verified_before_persistence_reader_archive_and_export(harness: Harness):
    paper_id = _extracted(harness)
    data = harness.ctx.store.papers.get_extraction(paper_id).data
    assert data["problem"]["verified"] and data["method"]["verified"]
    assert not data["prior_gap"]["verified"]
    for field in ("claims", "datasets", "metrics", "results"):
        assert data[field][0]["verified"]
    assert not any(item["verified"] for item in data["limitations"])
    assert data["questions"][0]["answer"]["verified"]
    assert data["questions"][1]["answer"] is None
    assert not data["questions"][2]["answer"]["verified"]
    assert data["highlights"][0]["bbox"] == list(BOX)
    assert data["highlights"][1]["bbox"] is None
    assert data["code_data"][0]["url"] == URL and data["code_data"][0]["url_verified"]
    assert data["code_data"][1]["url"] is None and not data["code_data"][1]["url_verified"]
    assert URL + "/fake" not in json.dumps(data)
    assert reader.paper_view(harness.ctx, paper_id).extraction == data
    assert not reader.paper_view(harness.ctx, paper_id).extraction_stale
    assert not knowledge.paper_concepts(harness.ctx, paper_id)[0].occurrence.stale
    assert run_stages(harness.ctx, ("assess", "score", "archive")).ok
    assert json.loads(harness.archive_files()[0].read_text())["extraction"] == data
    files = {entry.name: entry.content for entry in mine(harness.ctx)}
    exported = files[export_name("papers", paper_id)].decode()
    assert "模型内容：method" in exported and "模型内容：highlights" in exported
    assert "模型记录中的待核验引文；PDF 第 1 页" in exported
    assert "作者原文（已核验）；PDF 第 2 页" in exported
    assert harness.llm_calls("extract") == 1


def _change(harness: Harness, change: str, monkeypatch) -> None:
    if change == "budget":
        harness.ctx = replace(
            harness.ctx,
            settings=replace(harness.ctx.settings, assess_token_budget=3000),
        )
    elif change == "contract":
        monkeypatch.setattr(extract, "EXTRACTION_CONTRACT_VERSION", "next-contract")
    else:
        route = harness.ctx.llm.routes["extract"]
        changes = {
            "model": {"model": "other-model"},
            "provider": {"provider_version": "other-provider-settings"},
            "prompt": {"prompt": replace(route.prompt, text=route.prompt.text + "\nNew rule.")},
        }
        routes = {**harness.ctx.llm.routes, "extract": replace(route, **changes[change])}
        harness.ctx = replace(harness.ctx, llm=replace(harness.ctx.llm, routes=routes))


@pytest.mark.parametrize("change", ["budget", "contract", "model", "provider", "prompt"])
def test_configuration_change_only_marks_history_stale_without_reextracting(
    harness: Harness, monkeypatch, change: str
):
    paper_id = _extracted(harness)
    old = harness.ctx.store.papers.get_extraction(paper_id)
    _change(harness, change, monkeypatch)
    assert extract.extraction_is_stale(harness.ctx, paper_id)
    assert knowledge.paper_concepts(harness.ctx, paper_id)[0].occurrence.stale
    assert not tuple(extract.ExtractStage().plan(harness.ctx))
    assert harness.run("extract").ok
    assert harness.ctx.store.papers.get_extraction(paper_id) == old
    assert harness.llm_calls("extract") == 1


@pytest.mark.parametrize("change", ["budget", "contract", "model", "provider", "prompt"])
def test_old_queued_input_is_skipped_before_model_call(harness: Harness, monkeypatch, change: str):
    paper_id = _ready(harness)
    (old,) = tuple(extract.ExtractStage().plan(harness.ctx))
    _change(harness, change, monkeypatch)
    extract.ExtractStage().process(harness.ctx, old.payload)
    assert harness.llm_calls("extract") == 0
    assert harness.ctx.store.papers.get_extraction(paper_id) is None
    (current,) = tuple(extract.ExtractStage().plan(harness.ctx))
    assert current.key != old.key


def test_contract_change_marks_history_stale_even_without_model_configuration(harness, monkeypatch):
    paper_id = _extracted(harness)
    harness.ctx = replace(harness.ctx, llm=replace(harness.ctx.llm, routes={}))
    assert not extract.extraction_is_stale(harness.ctx, paper_id)
    monkeypatch.setattr(extract, "EXTRACTION_CONTRACT_VERSION", "next-contract")
    assert extract.extraction_is_stale(harness.ctx, paper_id)
    assert harness.llm_calls("extract") == 1


@pytest.mark.parametrize("change", ["budget", "contract", "model", "provider", "prompt"])
def test_explicit_dead_retry_recovers_first_extraction_with_the_current_configuration(
    harness: Harness, monkeypatch, change: str
):
    paper_id = _ready(harness)
    harness.llm.fail_tasks["extract"] = [PermanentError("credentials need repair")]
    assert not harness.run("extract").ok
    assert harness.ctx.store.papers.get_fulltext(paper_id).status == "failed"
    _change(harness, change, monkeypatch)
    calls = harness.llm_calls("extract")
    assert harness.run("extract").ok
    assert harness.llm_calls("extract") == calls
    assert harness.ctx.store.papers.get_extraction(paper_id) is None
    assert harness.ctx.store.jobs.retry_dead(harness.ctx.now_iso()) == 1
    assert harness.run("extract").ok
    result = harness.ctx.store.papers.get_extraction(paper_id)
    assert result is not None and harness.ctx.store.papers.get_fulltext(paper_id).status == "parsed"
    assert result.extractor_version == extract.extraction_producer_version(harness.ctx)
    assert harness.llm_calls("extract") == calls + 1
    assert harness.run("extract").ok
    assert harness.llm_calls("extract") == calls + 1


def test_automatic_retry_does_not_authorize_borrowing_changed_configuration(harness, monkeypatch):
    paper_id = _ready(harness)
    harness.llm.fail_tasks["extract"] = [TransientError("try later")]
    assert not harness.run("extract").ok
    (old,) = harness.ctx.store.jobs.list_by_status("pending")
    assert "retry_current" not in old.payload
    _change(harness, "model", monkeypatch)
    calls = harness.llm_calls("extract")
    extract.ExtractStage().process(harness.ctx, old.payload)
    assert harness.llm_calls("extract") == calls
    assert harness.ctx.store.papers.get_extraction(paper_id) is None


def test_explicit_retry_marker_cannot_reextract_existing_history(harness, monkeypatch):
    paper_id = _extracted(harness)
    state = harness.ctx.store.papers.get_fulltext(paper_id)
    harness.ctx.store.papers.save_fulltext(replace(state, status="failed"), harness.ctx.now_iso())
    _change(harness, "model", monkeypatch)
    extract.ExtractStage().process(
        harness.ctx,
        {"paper_id": paper_id, "input_hash": "old", "producer": "old", "retry_current": True},
    )
    assert harness.llm_calls("extract") == 1
