"""适配器统一契约。新适配器加入这里的参数列表，就要通过同样的测试。"""

import json
from pathlib import Path

import pytest

from litfactory.adapters.blobstore.local import LocalBlobStore
from litfactory.adapters.fakes.fulltext import FakeFullText
from litfactory.adapters.fakes.llm import FakeLLM
from litfactory.adapters.fakes.parser import FakeParser
from litfactory.adapters.fakes.publisher import FakePublisher
from litfactory.adapters.fakes.source import FakeSource
from litfactory.adapters.renderers.plain_text import PlainTextRenderer
from litfactory.domain.digest import DigestView, PublicCard
from litfactory.domain.document import NormalizedDocument
from litfactory.domain.errors import SchemaChanged
from litfactory.domain.identity import Alias
from litfactory.domain.paper import SourceRecord
from litfactory.domain.query import AuthorWatch, KeywordSearch
from litfactory.ports.fulltext import FullTextRequest
from litfactory.ports.llm import LLMRequest
from litfactory.ports.publisher import Rendered
from litfactory.ports.source import FetchResult, Source

RECORDS = [{"title": "One", "doi": "10.1/one"}, {"title": "Two", "doi": "10.1/two"}]


@pytest.fixture
def source(tmp_path: Path) -> Source:
    path = tmp_path / "records.json"
    path.write_text(json.dumps(RECORDS), encoding="utf-8")
    return FakeSource("contract", path)


QUERY = KeywordSearch(terms=("x",))


def test_source_declares_capabilities_and_returns_validated_records(source: Source):
    assert source.name
    assert KeywordSearch in source.capabilities
    result = source.fetch(QUERY, None)
    assert isinstance(result, FetchResult)
    assert all(isinstance(record, SourceRecord) for record in result.records)


def test_source_cursor_only_returns_new_records(source: Source):
    first = source.fetch(QUERY, None)
    assert [r.title for r in first.records] == ["One", "Two"]
    assert source.fetch(QUERY, first.next_cursor).records == ()


def test_source_rejects_query_it_did_not_declare(source: Source):
    assert AuthorWatch not in source.capabilities
    with pytest.raises(ValueError, match="不支持"):
        source.fetch(AuthorWatch(names=("A",)), None)


def test_source_reports_malformed_data_as_schema_changed(tmp_path: Path):
    path = tmp_path / "bad.json"
    path.write_text(json.dumps([{"title": ""}]), encoding="utf-8")
    with pytest.raises(SchemaChanged):
        FakeSource("bad", path).fetch(QUERY, None)


def test_fulltext_returns_pdf_or_none():
    provider = FakeFullText()
    aliases = (Alias("doi", "10.1/x"),)
    found = provider.fetch(FullTextRequest("P", "Paper", aliases))
    assert found is not None
    assert found.content.startswith(b"%PDF-")
    assert found.license_basis in {"open", "subscribed", "user_supplied"}
    assert provider.fetch(FullTextRequest("P", "NoPDF", aliases)) is None


def test_parser_returns_normalized_document_that_round_trips():
    pdf = FakeFullText().fetch(FullTextRequest("P", "Paper", ()))
    assert pdf is not None
    document = FakeParser().parse(pdf.content, "a" * 64).document
    assert document.source_pdf_sha256 == "a" * 64
    assert document.parser_id == FakeParser.parser_id
    assert NormalizedDocument.from_dict(document.to_dict()) == document


def test_llm_reports_model_tokens_and_cost():
    response = FakeLLM().complete(LLMRequest("triage", "m", "sys", "user", {}))
    assert response.model == "m"
    assert response.cost_usd >= 0
    assert response.input_tokens > 0


def test_publisher_returns_receipt_and_can_write_output(tmp_path: Path):
    output = tmp_path / "out" / "sent.jsonl"
    receipt = FakePublisher(output).publish(Rendered("t", "正文"))
    assert receipt.external_id
    assert json.loads(output.read_text(encoding="utf-8"))["body"] == "正文"


def test_renderer_handles_public_cards_without_private_fields():
    card = PublicCard(
        "T", ("A",), None, None, "10.1/x", 90, "must_read", None, None, (), None, "必读"
    )
    view = DigestView("D", "主题", "2026-11-03", "public", (card,))
    rendered = PlainTextRenderer().render(view)
    assert "[90分·必读] T" in rendered.body
    assert "https://doi.org/10.1/x" in rendered.body
    assert "摘要" not in rendered.body


@pytest.fixture
def blobs(tmp_path: Path) -> LocalBlobStore:
    return LocalBlobStore(tmp_path / "blobs")


def test_blobstore_round_trip_and_delete(blobs: LocalBlobStore):
    assert not blobs.exists("a/b.bin")
    blobs.put("a/b.bin", b"data")
    assert blobs.exists("a/b.bin")
    assert blobs.get("a/b.bin") == b"data"
    blobs.put("a/b.bin", b"new")
    assert blobs.get("a/b.bin") == b"new"
    blobs.delete("a/b.bin")
    blobs.delete("a/b.bin")
    assert not blobs.exists("a/b.bin")


@pytest.mark.parametrize("key", ["../escape", "/etc/passwd", "a/../../b", ""])
def test_blobstore_rejects_path_traversal(blobs: LocalBlobStore, key: str):
    with pytest.raises(ValueError, match="非法"):
        blobs.put(key, b"x")


def test_blobstore_leaves_no_temp_files(blobs: LocalBlobStore, tmp_path: Path):
    blobs.put("x/y.bin", b"data")
    names = [p.name for p in (tmp_path / "blobs").rglob("*") if p.is_file()]
    assert names == ["y.bin"]
