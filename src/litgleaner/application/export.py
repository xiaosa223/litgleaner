"""只读取 SQLite 与已有 blob，产出可携带的开放格式文件。"""

import json
import re
from collections.abc import Iterator
from hashlib import sha256
from itertools import chain
from typing import Any

from litgleaner.application.context import AppContext
from litgleaner.application.documents import pdf_key
from litgleaner.application.export_format import label
from litgleaner.application.export_markdown import concept_markdown, paper_markdown
from litgleaner.domain.errors import LitGleanerError
from litgleaner.domain.export import ExportFile, export_name
from litgleaner.domain.export_references import render_references
from litgleaner.ports.export import ExportPaper, ExportSnapshot


def _text(name: str, text: str) -> ExportFile:
    return ExportFile(name, text.encode("utf-8"))


def _json(name: str, value: Any) -> ExportFile:
    return _text(name, json.dumps(value, ensure_ascii=False, indent=2) + "\n")


def _readme(snapshot: ExportSnapshot, include_pdfs: bool) -> str:
    lines = [
        "# 拾穗个人数据导出",
        "导出来自数据库快照，只供阅读和迁移，不反向导入。",
        "manifest.json 最后写入，列出其余文件的大小与 SHA-256；缺少清单表示导出未完成。",
        "作者原文、模型生成及用户所写分别标明；未核验的用户摘录与模型引文保留核验说明。",
        "正文显示的 PDF 页码从 1 开始；原始 JSON 的页码保持数据库的零基编号。",
        "保存全部论文、提取和评估历史、有效笔记和高亮、问答及概念记录。",
        "当前版本尚未提供用户回答卡片；相关章节明确注明，不以模型问答代替。",
        f"PDF 导出：{'包含已有且校验通过的原 PDF' if include_pdfs else '默认不包含 PDF'}。"
        "缺失与无法读取的 PDF 见 manifest.json。",
        "references.bib 与 references.csl.json 只收录具有有效标识符的来源元数据；"
        "其余论文列在 NEEDS-VERIFICATION.md，论文 Markdown 仍完整导出。",
        "## 论文",
    ]
    if include_pdfs:
        lines.insert(-1, "已导出的原 PDF 见 [PDF 列表](PDFS.md)。")
    lines += [
        f"- [{label(item.paper.title)}]({export_name('papers', item.paper.id)})"
        for item in snapshot.papers
    ]
    lines += ["\n## 概念"]
    lines += [
        f"- [{label(item.concept.name)}]({export_name('concepts', item.concept.id)})"
        for item in snapshot.concepts
    ]
    return "\n\n".join(lines) + "\n"


def _pdf(ctx: AppContext, item: ExportPaper) -> tuple[ExportFile | None, str | None]:
    digest = item.pdf_sha256
    if digest is None:
        return None, "pdf_unavailable"
    if re.fullmatch(r"[0-9a-f]{64}", digest) is None:
        return None, "pdf_invalid_key"
    try:
        content = ctx.blobs.get(pdf_key(digest))
    except (OSError, ValueError, LitGleanerError):
        return None, "pdf_unreadable"
    if sha256(content).hexdigest() != digest or b"%PDF-" not in content[:1024]:
        return None, "pdf_integrity_failed"
    return ExportFile(export_name("pdfs", item.paper.id, "pdf"), content), None


def _content(snapshot: ExportSnapshot, include_pdfs: bool) -> Iterator[ExportFile]:
    yield _text("README.md", _readme(snapshot, include_pdfs))
    for item in snapshot.papers:
        yield _text(export_name("papers", item.paper.id), paper_markdown(item))
    for concept in snapshot.concepts:
        yield _text(export_name("concepts", concept.concept.id), concept_markdown(concept))
    refs = render_references(tuple((item.paper, item.aliases) for item in snapshot.papers))
    yield _text("references.bib", refs.bibtex)
    yield _text("references.csl.json", refs.csl_json)
    lines = ["# 需要人工核对", "这些论文的标识符缺失、无效或冲突，未进入正式引用文件。"]
    lines += [f"- [{label(id_)}]({export_name('papers', id_)})" for id_ in refs.unverified_ids]
    lines += [f"- {label(warning)}" for warning in refs.warnings]
    yield _text("NEEDS-VERIFICATION.md", "\n\n".join(lines) + "\n")


def _pdf_entries(
    ctx: AppContext, snapshot: ExportSnapshot, warnings: list[dict[str, str]]
) -> Iterator[ExportFile]:
    index = ["# 已导出的原 PDF", "只复制本地已有且哈希与文件头校验通过的 PDF。"]
    for item in snapshot.papers:
        pdf, warning = _pdf(ctx, item)
        if warning:
            warnings.append({"paper_id": item.paper.id, "code": warning})
        if pdf:
            index.append(f"- [{label(item.paper.title)}]({pdf.name})")
            yield pdf
    yield _text("PDFS.md", "\n\n".join(index) + "\n")


def mine(ctx: AppContext, *, include_pdfs: bool = False) -> Iterator[ExportFile]:
    snapshot = ctx.store.export_snapshot()
    manifest: dict[str, Any] = {
        "format_version": 1,
        "created_at": ctx.now_iso(),
        "papers": len(snapshot.papers),
        "concepts": len(snapshot.concepts),
        "include_pdfs": include_pdfs,
        "files": [],
        "warnings": [],
        "unavailable_sections": {"answer_cards": "当前版本尚未提供用户回答卡片功能"},
    }
    attachments = _pdf_entries(ctx, snapshot, manifest["warnings"]) if include_pdfs else ()
    for entry in chain(_content(snapshot, include_pdfs), attachments):
        manifest["files"].append(_file_record(entry))
        yield entry
    yield _json("manifest.json", manifest)


def _file_record(entry: ExportFile) -> dict[str, Any]:
    return {
        "name": entry.name,
        "bytes": len(entry.content),
        "sha256": sha256(entry.content).hexdigest(),
    }
