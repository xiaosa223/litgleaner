"""用户上传文档的本地文字抽取（ADR 0012）：PDF、DOCX、Markdown、TXT。"""

import io
import zipfile

import pytest
from tests.helpers.pdf import make_pdf

from litgleaner.adapters.documents import local as document_module
from litgleaner.adapters.documents.local import LocalDocumentText
from litgleaner.domain.errors import InvalidRequest

W = "http://schemas.openxmlformats.org/wordprocessingml/2006/main"


def _docx(paragraphs: list[str], *, extra: bytes = b"") -> bytes:
    body = "".join(
        f'<w:p><w:r><w:t xml:space="preserve">{text}</w:t></w:r></w:p>' for text in paragraphs
    )
    xml = f'<?xml version="1.0"?><w:document xmlns:w="{W}"><w:body>{body}</w:body></w:document>'
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as archive:
        archive.writestr("[Content_Types].xml", "<Types/>")
        archive.writestr("word/document.xml", xml.encode("utf-8") + extra)
    return buffer.getvalue()


def _zip(files: dict[str, str]) -> bytes:
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as archive:
        for name, data in files.items():
            archive.writestr(name, data)
    return buffer.getvalue()


def _code(error: pytest.ExceptionInfo[InvalidRequest]) -> str:
    return str(error.value)


def test_pdf_text_layer_is_extracted_page_by_page():
    pdf = make_pdf([["Project brief: EEG foundation models"], ["Goal: few-shot transfer"]])
    text = LocalDocumentText().extract("brief.pdf", pdf)
    assert "EEG foundation models" in text
    assert "few-shot transfer" in text
    assert text.index("EEG") < text.index("few-shot")


def test_scanned_pdf_without_text_is_reported_not_silently_empty():
    with pytest.raises(InvalidRequest) as error:
        LocalDocumentText().extract("scan.pdf", make_pdf([[]]))
    assert _code(error) == "document_no_text"


def test_docx_paragraphs_keep_order_and_line_breaks():
    text = LocalDocumentText().extract("任务书.docx", _docx(["研究背景：脑电", "研究目标：迁移"]))
    assert text.splitlines() == ["研究背景：脑电", "研究目标：迁移"]


def test_docx_is_recognised_by_content_even_with_wrong_name():
    assert "研究背景" in LocalDocumentText().extract("upload", _docx(["研究背景"]))


@pytest.mark.parametrize("encoding", ["utf-8", "utf-8-sig", "gb18030"])
def test_markdown_and_text_decode_common_chinese_encodings(encoding: str):
    raw = "# 比赛说明\n\n赛题：低资源语音识别".encode(encoding)
    for name in ("readme.md", "说明.txt"):
        text = LocalDocumentText().extract(name, raw)
        assert "赛题：低资源语音识别" in text
        assert not text.startswith("﻿")


@pytest.mark.parametrize(
    ("name", "content"),
    [
        ("old.doc", b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1" + b"\x00" * 64),
        ("photo.png", b"\x89PNG\r\n\x1a\n" + b"\x00" * 64),
        ("slides.pptx", _zip({"ppt/presentation.xml": "<p/>"})),
        ("data.bin", bytes(range(256))),
    ],
)
def test_unsupported_formats_are_rejected_with_a_fixed_code(name: str, content: bytes):
    with pytest.raises(InvalidRequest) as error:
        LocalDocumentText().extract(name, content)
    assert _code(error) == "document_type_unsupported"


def test_docx_expanding_beyond_limit_is_rejected():
    bomb = _docx(["x"], extra=b" " * (60 * 1024 * 1024))
    assert len(bomb) < 1024 * 1024  # 压缩后很小
    with pytest.raises(InvalidRequest) as error:
        LocalDocumentText().extract("bomb.docx", bomb)
    assert _code(error) == "document_too_large"


def test_docx_with_entity_declarations_is_rejected():
    xml = (
        '<?xml version="1.0"?><!DOCTYPE d [<!ENTITY a "aaaaaaaaaa">]>'
        f'<w:document xmlns:w="{W}"><w:body><w:p><w:r><w:t>&a;</w:t></w:r></w:p></w:body>'
        "</w:document>"
    )
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as archive:
        archive.writestr("word/document.xml", xml)
    with pytest.raises(InvalidRequest) as error:
        LocalDocumentText().extract("entities.docx", buffer.getvalue())
    assert _code(error) == "document_type_unsupported"


def test_corrupt_pdf_is_rejected():
    with pytest.raises(InvalidRequest) as error:
        LocalDocumentText().extract("broken.pdf", b"%PDF-1.4\nnot really a pdf")
    assert _code(error) in {"document_unreadable", "document_no_text"}


def test_docx_stops_collecting_text_after_the_limit():
    paragraphs = ["段落" * 50] * 5000
    text = LocalDocumentText().extract("long.docx", _docx(paragraphs))
    assert len(text) <= 200_000 + 200


def test_deeply_nested_pdf_errors_are_classified(monkeypatch):
    def boom(*_args, **_kwargs):
        raise RecursionError

    monkeypatch.setattr(document_module, "PdfReader", boom)
    with pytest.raises(InvalidRequest) as error:
        LocalDocumentText().extract("deep.pdf", b"%PDF-1.4")
    assert _code(error) == "document_unreadable"
