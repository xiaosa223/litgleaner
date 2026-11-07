"""对比 MinerU VLM 和 PDF 直接提取，对同一批论文测量提取质量。

用法：
    source .env.example   # 从钥匙串注入 MINERU_API_TOKEN
    uv run python scripts/probe_parsers.py data/probe

已解析过的论文会读取缓存，重跑不会重复消耗 MinerU 额度。
"""

import hashlib
import html
import itertools
import json
import logging
import os
import re
import sys
import time
import xml.etree.ElementTree as ET
from pathlib import Path
from typing import Any

from litgleaner.adapters.parsers.mineru_cloud import MinerUCloudParser
from litgleaner.adapters.parsers.mineru_content_list import to_normalized_document
from litgleaner.adapters.parsers.pdf_text import PdfTextParser
from litgleaner.domain.chunker import ChunkedDocument, ChunkerConfig, chunk_document
from litgleaner.domain.document import NormalizedDocument
from litgleaner.domain.evidence import select_chunks

logger = logging.getLogger("probe")
CJK = re.compile(r"[㐀-鿿]")
NUMBER = re.compile(r"\d+(?:\.\d+)?%?")
LLM_BUDGET = 6000
PREVIEW_CHARS = 60


def ground_truth(root: Path) -> dict[str, str]:
    truth: dict[str, str] = {}
    atom = {"a": "http://www.w3.org/2005/Atom"}
    for entry in ET.parse(root / "arxiv_meta.xml").getroot().findall("a:entry", atom):
        arxiv_id = entry.findtext("a:id", "", atom).rsplit("/", 1)[-1]
        truth[f"arxiv-{arxiv_id}"] = " ".join(entry.findtext("a:summary", "", atom).split())
    page = (root / "crad.html").read_text(encoding="utf-8")
    match = re.search(r'<meta name="dc.description" content="([^"]*)"', page)
    if match:
        text = re.sub(r"<[^>]+>", "", html.unescape(match.group(1)))
        truth["crad-202220410"] = " ".join(text.split())
    return truth


def units(text: str) -> list[str]:
    """英文按词，中文按相邻两字，用于计算覆盖率。"""
    if CJK.search(text):
        chars = [ch for ch in text if CJK.match(ch)]
        return [a + b for a, b in itertools.pairwise(chars)]
    return re.findall(r"[a-z0-9]+", text.casefold())


def recall(truth: str, text: str) -> float:
    expected = units(truth)
    if not expected:
        return 0.0
    present = set(units(text))
    return round(sum(unit in present for unit in expected) / len(expected), 3)


def squash(text: str) -> str:
    return re.sub(r"\s+", "", text)


def section_text(chunked: ChunkedDocument, section: str) -> str:
    return " ".join(c.text for c in chunked.chunks if c.section_type == section)


def measure(document: NormalizedDocument, truth: str, seconds: float) -> dict[str, Any]:
    chunked = chunk_document(document, ChunkerConfig())
    full = " ".join(c.text for c in chunked.chunks)
    picked = select_chunks(chunked.chunks, LLM_BUDGET)
    numbers = sorted(set(NUMBER.findall(truth)))
    kinds: dict[str, int] = {}
    for block in document.blocks:
        kinds[block.kind] = kinds.get(block.kind, 0) + 1
    return {
        "seconds": round(seconds, 1),
        "quality": chunked.quality,
        "warnings": list(chunked.warnings),
        "blocks": kinds,
        "chunks": len(chunked.chunks),
        "tokens": sum(c.token_count for c in chunked.chunks),
        "sections": sorted({c.section_type for c in chunked.chunks}),
        "headings": [b.text for b in document.blocks if b.kind == "heading"][:40],
        "abstract_recall": recall(truth, section_text(chunked, "abstract")),
        "abstract_recall_anywhere": recall(truth, full),
        "abstract_numbers_kept": [n for n in numbers if squash(n) in squash(full)],
        "abstract_numbers_total": numbers,
        "conclusion_tokens": sum(
            c.token_count for c in chunked.chunks if c.section_type == "conclusion"
        ),
        "references": len(chunked.references),
        "llm_input_tokens": sum(c.token_count for c in picked),
        "llm_input_sections": sorted({c.section_type for c in picked}),
    }


def mineru_document(pdf: bytes, sha: str, cache: Path) -> tuple[NormalizedDocument, float]:
    if cache.is_file():
        items = json.loads(cache.read_text(encoding="utf-8"))
        return to_normalized_document(items, sha, "mineru_cloud@vlm"), float("nan")
    parser = MinerUCloudParser(
        os.environ.get("MINERU_API_TOKEN", ""),
        {"state_dir": str(cache.parent / "state"), "timeout_seconds": 900, "language": "ch"},
    )
    start = time.monotonic()
    result = parser.parse(pdf, sha)
    elapsed = time.monotonic() - start
    assert result.raw is not None
    cache.parent.mkdir(parents=True, exist_ok=True)
    cache.write_bytes(result.raw)
    return result.document, elapsed


def minimized(items: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """测试样本只保留结构：标题全文，正文截断。不把论文全文放进仓库。"""
    keep = {"type", "text_level", "page_idx", "bbox", "text_format", "sub_type"}
    out = []
    for item in items:
        slim = {key: item[key] for key in keep if key in item}
        if "text" in item:
            text = str(item["text"])
            slim["text"] = text if item.get("text_level") else text[:PREVIEW_CHARS]
        for key in ("table_caption", "chart_caption", "code_caption"):
            if key in item:
                slim[key] = item[key]
        out.append(slim)
    return out


def main(root: Path) -> None:
    truth = ground_truth(root)
    report: dict[str, Any] = {}
    for pdf_path in sorted((root / "pdfs").glob("*.pdf")):
        name = pdf_path.stem
        pdf = pdf_path.read_bytes()
        sha = hashlib.sha256(pdf).hexdigest()
        logger.info("处理 %s", name)
        start = time.monotonic()
        baseline = PdfTextParser().parse(pdf, sha).document
        baseline_seconds = time.monotonic() - start
        cache = root / "mineru" / f"{name}.content_list.json"
        vlm, vlm_seconds = mineru_document(pdf, sha, cache)
        expected = truth.get(name, "")
        report[name] = {
            "pages": len({b.page for b in baseline.blocks}) or None,
            "truth_abstract_chars": len(expected),
            "pdf_text": measure(baseline, expected, baseline_seconds),
            "mineru_vlm": measure(vlm, expected, vlm_seconds),
        }
        fixture = root / "fixtures" / f"{name}.content_list.min.json"
        fixture.parent.mkdir(parents=True, exist_ok=True)
        items = json.loads(cache.read_text(encoding="utf-8"))
        fixture.write_text(json.dumps(minimized(items), ensure_ascii=False), encoding="utf-8")
    (root / "report.json").write_text(
        json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    logger.info("完成，结果见 %s", root / "report.json")


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(message)s")
    for noisy in ("httpx", "httpcore"):  # 会记录带签名的完整 URL
        logging.getLogger(noisy).setLevel(logging.WARNING)
    main(Path(sys.argv[1]))
