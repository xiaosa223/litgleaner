"""真实解析流水线将原始参考文献与全文状态原子保存，不调用模型。"""

from dataclasses import replace

from litgleaner.application import citations
from litgleaner.application.citations import for_paper
from litgleaner.application.stages.fulltext import ParseStage
from litgleaner.domain.document import Block
from litgleaner.domain.identity import Alias
from tests.e2e.conftest import Harness, paper


def _prepare(harness: Harness, monkeypatch):
    harness.write_papers([paper("Parent", 5), paper("Target", 5)])
    for stage in ("discover", "triage", "acquire"):
        assert harness.run(stage).ok
    parent = next(
        iter(harness.ctx.store.papers.find_ids_by_aliases([Alias("doi", "10.1000/parent")]))
    )
    target = next(
        iter(harness.ctx.store.papers.find_ids_by_aliases([Alias("doi", "10.1000/target")]))
    )
    original = harness.ctx.parser.parse

    def parse(pdf, sha256):
        result = original(pdf, sha256)
        if result.document.title != "Parent":
            return result
        blocks = (
            *result.document.blocks,
            Block("heading", "References", 2),
            Block("reference", "[1] Author. Target. doi:10.1000/target.", 2, (10, 20, 90, 50)),
            Block("reference", "[2] Unidentified original reference.", 2),
        )
        return replace(result, document=replace(result.document, blocks=blocks))

    monkeypatch.setattr(harness.ctx.parser, "parse", parse)
    return parent, target


def test_parse_preserves_raw_references_page_and_bbox_without_extra_model_calls(
    harness, monkeypatch
):
    parent, target = _prepare(harness, monkeypatch)
    before = harness.llm_calls()
    assert harness.run("parse").ok
    result = for_paper(harness.ctx, parent)
    assert len(result.outgoing) == 2
    linked = result.outgoing[0]
    assert linked.cited_paper_id == target and linked.cited_title == "Target"
    assert linked.citation.raw_text == "[1] Author. Target. doi:10.1000/target."
    assert linked.citation.page == 2 and linked.citation.bbox == (10, 20, 90, 50)
    assert linked.citation.contexts == () and linked.citation.influential is None
    assert result.outgoing[1].cited_paper_id is None
    assert for_paper(harness.ctx, target).incoming[0].citation.citing_paper_id == parent
    ParseStage().process(harness.ctx, {"paper_id": parent})
    assert len(for_paper(harness.ctx, parent).outgoing) == 2
    assert harness.llm_calls() == before


def test_reference_failure_rolls_back_fulltext_and_index_but_not_other_papers(harness, monkeypatch):
    parent, target = _prepare(harness, monkeypatch)

    def fail(_citation):
        raise RuntimeError("simulated citation write failure")

    monkeypatch.setattr(harness.ctx.store.citations, "add", fail)
    assert not harness.run("parse").ok
    assert harness.ctx.store.papers.get_fulltext(parent).status == "available"
    assert harness.ctx.store.papers.get_fulltext(parent).doc_key is None
    assert harness.ctx.store.papers.get_fulltext(target).status == "parsed"
    assert not harness.ctx.store.citations.outgoing(parent)
    assert not [
        hit
        for hit in harness.ctx.store.search.find("Unidentified", 50)
        if hit.paper_id == parent and hit.kind == "fulltext"
    ]


def test_legacy_archive_can_be_explicitly_indexed_without_reparsing_or_model_cost(
    harness, monkeypatch
):
    parent, _ = _prepare(harness, monkeypatch)
    with monkeypatch.context() as patch:
        patch.setattr("litgleaner.application.stages.fulltext.ingest_parsed", lambda *args: None)
        assert harness.run("parse").ok
    assert not for_paper(harness.ctx, parent).outgoing
    before = harness.llm_calls()

    def never_parse(*args):
        raise AssertionError("旧归档重建不能重新解析 PDF")

    monkeypatch.setattr(harness.ctx.parser, "parse", never_parse)
    assert len(citations.index_parsed(harness.ctx, parent).outgoing) == 2
    assert len(citations.index_parsed(harness.ctx, parent).outgoing) == 2
    assert harness.llm_calls() == before
