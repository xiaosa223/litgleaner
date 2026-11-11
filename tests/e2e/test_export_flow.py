"""完整离线导出：字段来源、全部历史、可选 PDF 和可校验清单。"""

import json
from dataclasses import replace
from hashlib import sha256

import pytest

from litgleaner.application.documents import pdf_key
from litgleaner.application.export import mine
from litgleaner.application.stages.discover import resolve_record
from litgleaner.domain.export import export_name
from litgleaner.domain.knowledge import ConceptOccurrence
from litgleaner.domain.paper import SourceRecord
from litgleaner.domain.qa import Answer, Citation, Question, Statement
from litgleaner.domain.reading import NewAnnotation
from litgleaner.ports.store import FullTextState


def _paper(harness, title="An original title", **fields):
    record = SourceRecord(title=title, abstract="作者摘要", **fields)
    return resolve_record(harness.ctx.store.papers, record, harness.ctx.now_iso())


def _files(ctx, **options):
    return {entry.name: entry.content for entry in mine(ctx, **options)}


def _markdown(files, paper_id):
    return files[export_name("papers", paper_id)].decode()


def test_empty_export_is_useful_without_models_or_budget_and_does_not_mutate(harness):
    ctx = replace(harness.ctx, llm=replace(harness.ctx.llm, routes={}, daily_budget_usd=0))
    before = ctx.store.stats()
    files = _files(ctx)
    assert set(files) == {
        "README.md",
        "references.bib",
        "references.csl.json",
        "NEEDS-VERIFICATION.md",
        "manifest.json",
    }
    assert json.loads(files["references.csl.json"]) == []
    manifest = json.loads(files["manifest.json"])
    assert manifest["papers"] == manifest["concepts"] == 0
    assert manifest["unavailable_sections"]["answer_cards"]
    assert ctx.store.stats() == before and harness.llm_calls() == 0
    assert not ctx.store.jobs.list_by_status("pending")


def test_all_papers_and_versions_export_with_explicit_field_sources(harness):
    ctx = harness.ctx
    id_ = _paper(harness, doi="10.1000/export")
    other = _paper(harness, "Unsorted untriaged paper")
    for index in range(2):
        ctx.store.papers.insert_extraction(
            id_,
            f"input-{index}",
            f"version-{index}",
            {
                "summary": f"模型摘要{index}",
                "conclusion": "模型结论",
                "limitations": [
                    {
                        "text": "模型概括作者局限",
                        "source": "author",
                        "quote": "核验原文局限",
                        "verified": True,
                    }
                ],
            },
            ctx.now_iso(),
        )
    ctx.store.reader.add_annotation(
        NewAnnotation(id_, "highlight", 0, None, "我的选中文本", "用户所写的笔记"), ctx.now_iso()
    )
    deleted = ctx.store.reader.add_annotation(
        NewAnnotation(id_, "note", None, None, None, "deleted-secret"), ctx.now_iso()
    )
    ctx.store.reader.delete_annotation(deleted.id, ctx.now_iso())
    before = ctx.store.stats()
    files = _files(ctx)
    text = _markdown(files, id_)
    assert "模型摘要0" in text and "模型摘要1" in text and "version-0" in text
    assert "作者原文（已核验）；页码未记录" in text
    assert "作者原文（用户选中文本，未单独核验）" in text
    assert "PDF 第 1 页" in text and "用户所写的笔记" in text
    assert not any(b"deleted-secret" in value for value in files.values())
    assert export_name("papers", other) in files
    assert ctx.store.stats() == before and harness.llm_calls() == 0
    assert json.loads(files["references.csl.json"])[0]["DOI"] == "10.1000/export"
    assert other in files["NEEDS-VERIFICATION.md"].decode()


def test_user_questions_model_answers_and_original_quotes_are_distinct_without_history_limit(
    harness,
):
    ctx = harness.ctx
    id_ = _paper(harness)
    for index in range(60):
        question = Question(
            f"q{index}",
            id_,
            f"r{index}",
            f"用户问题 {index}",
            "选中原文",
            0,
            "zh-CN",
            ctx.now_iso(),
        )
        ctx.store.qa.add_question(question)
        ctx.store.qa.add_answer(
            Answer(
                f"a{index}",
                id_,
                question.id,
                (Statement(f"模型推断 {index}", (Citation(id_, 2, "作者论据"),), True),),
                "saved-model",
                "saved-producer",
                "saved-input",
                ctx.now_iso(),
            )
        )
    files = _files(ctx)
    text = _markdown(files, id_)
    assert len(ctx.store.qa.questions(id_)) == 50
    for index in range(60):
        assert f"用户问题 {index}" in text and f"模型推断 {index}" in text
    assert "模型推断" in text and "作者原文" in text and "PDF 第 3 页" in text
    assert "saved-producer" in text and "当前版本尚未提供用户回答卡片" in text


def test_concepts_export_original_definitions_locations_and_no_fabricated_mastery(harness):
    ctx = harness.ctx
    id_ = _paper(harness)
    concept = ctx.store.knowledge.register("Original Concept", "en", ctx.now_iso())
    ctx.store.knowledge.add_alias(concept.id, "原始概念", "zh-CN", ctx.now_iso())
    occurrence = ConceptOccurrence(
        concept.id,
        id_,
        "core",
        "The verified original definition.",
        0,
        (1, 2, 3, 4),
        True,
        "input",
        "producer",
        ctx.now_iso(),
    )
    ctx.store.knowledge.add_occurrence(occurrence)
    text = _files(ctx)[export_name("concepts", concept.id)].decode()
    assert "作者原文定义（已核验）" in text and occurrence.definition in text
    assert "PDF 第 1 页" in text and "模型标注的角色：core" in text
    assert "原始概念" in text and "producer" in text and "掌握程度" not in text


def test_raw_html_and_fences_cannot_escape_literal_user_content(harness):
    ctx = harness.ctx
    id_ = _paper(harness, "<script>alert(1)</script>\n# escaped header")
    body = "```\n<script>user text</script>\n``````\nexact original"
    ctx.store.reader.add_annotation(
        NewAnnotation(id_, "note", None, None, None, body), ctx.now_iso()
    )
    files = _files(ctx)
    text = _markdown(files, id_)
    assert text.startswith("# &lt;script&gt;alert\\(1\\)&lt;/script&gt; \\# escaped header")
    assert f"```````text\n{body}\n```````" in text
    assert all("<script>" not in name and ".." not in name for name in files)


def test_default_export_never_reads_pdf_and_manifest_hashes_match_files(harness, monkeypatch):
    id_ = _paper(harness)
    harness.ctx.store.papers.save_fulltext(FullTextState(id_, "available", "a" * 64), "t")
    monkeypatch.setattr(harness.ctx.blobs, "get", lambda _key: pytest.fail("default read PDF"))
    files = _files(harness.ctx)
    manifest = json.loads(files["manifest.json"])
    assert not manifest["include_pdfs"] and manifest["warnings"] == []
    assert {entry["name"] for entry in manifest["files"]} == files.keys() - {"manifest.json"}
    for entry in manifest["files"]:
        assert entry["bytes"] == len(files[entry["name"]])
        assert entry["sha256"] == sha256(files[entry["name"]]).hexdigest()


@pytest.mark.parametrize(
    "condition", ["valid", "missing", "hash_mismatch", "not_pdf", "invalid_key"]
)
def test_optional_pdfs_only_copy_verified_existing_bytes_and_report_missing(harness, condition):
    ctx = harness.ctx
    id_ = _paper(harness)
    content = b"%PDF-1.7\nexisting content" if condition != "not_pdf" else b"ordinary bytes"
    digest = sha256(content).hexdigest()
    key = digest if condition != "invalid_key" else "../unsafe"
    ctx.store.papers.save_fulltext(FullTextState(id_, "available", key), "t")
    if condition not in ("missing", "invalid_key"):
        ctx.blobs.put(
            pdf_key(digest), b"different bytes" if condition == "hash_mismatch" else content
        )
    files = _files(ctx, include_pdfs=True)
    manifest = json.loads(files["manifest.json"])
    pdf_name = export_name("pdfs", id_, "pdf")
    if condition == "valid":
        assert files[pdf_name] == content and not manifest["warnings"]
        assert pdf_name in files["PDFS.md"].decode()
    else:
        assert pdf_name not in files and pdf_name not in files["PDFS.md"].decode()
        assert manifest["warnings"][0]["paper_id"] == id_
    assert harness.llm_calls() == 0
