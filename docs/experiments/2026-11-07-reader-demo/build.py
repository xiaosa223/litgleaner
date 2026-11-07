"""构建领读器演示：把 MinerU 解析结果、页面图片和人工撰写的增强内容打包成一个本地网页。

用法（在仓库根目录）：
    python3 -I docs/experiments/2026-11-07-reader-demo/build.py \
        --content-list data/probe/mineru/arxiv-1706.03762v7.content_list.json \
        --pdf data/probe/pdfs/arxiv-1706.03762v7.pdf \
        --out data/reader-demo

构建前按架构文档第 12.1 节做两项自动检查，不通过就失败：
1. 每条“作者原话”都逐字存在于对应块的原文中；
2. 译文按句与原文对齐，句数必须一致；
3. 每句译文中术语表译名的命中率为 100%（英文原句出现的术语，对应译句必须用术语表的译名）。

原版视图的可选中文字层由浏览器里的 pdf.js 从 paper.pdf 生成。
"""

import argparse
import json
import re
import shutil
import subprocess
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
DROP = {"page_number", "aside_text", "header", "footer"}


def norm(text: str) -> str:
    return re.sub(r"\s+", " ", text).strip()


def block_text(item: dict) -> str:
    for key in ("text", "table_caption", "image_caption", "chart_caption", "code_body"):
        value = item.get(key)
        if value:
            return " ".join(value) if isinstance(value, list) else str(value)
    return ""


def load_blocks(path: Path) -> list[dict]:
    items = json.loads(path.read_text(encoding="utf-8"))
    blocks = []
    for i, item in enumerate(items):
        kind = item["type"]
        if kind in DROP:
            continue
        blocks.append(
            {
                "i": i,
                "type": kind,
                "page": item.get("page_idx", 0),
                "bbox": item.get("bbox"),
                "level": item.get("text_level"),
                "text": block_text(item),
                "table": item.get("table_body", ""),
            }
        )
    return blocks


def iter_quotes(node, path="augment"):
    if isinstance(node, dict):
        if "b" in node and "q" in node and isinstance(node["q"], str):
            yield path, node["b"], node["q"]
        for key, value in node.items():
            yield from iter_quotes(value, f"{path}.{key}")
    elif isinstance(node, list):
        for index, value in enumerate(node):
            yield from iter_quotes(value, f"{path}[{index}]")


def check_quotes(augment: dict, by_id: dict[int, dict]) -> list[str]:
    errors = []
    count = 0
    for path, b, quote in iter_quotes(augment):
        count += 1
        block = by_id.get(b)
        if block is None:
            errors.append(f"{path}: 块 {b} 不存在")
            continue
        haystack = norm(block["text"] + " " + block["table"])
        if norm(quote) not in haystack:
            errors.append(f"{path}: 块 {b} 中找不到原话：{quote[:60]}")
    print(f"原话核验：{count - len(errors)}/{count} 通过")
    return errors


def term_hits(source: str, glossary: list[dict]) -> list[dict]:
    """找出原文中出现的术语；被更长术语覆盖的短术语不计（如 self-attention 里的 attention）。"""
    spans = []
    for entry in glossary:
        for pattern in entry["en"]:
            for match in re.finditer(rf"\b{pattern}\b", source, flags=re.IGNORECASE):
                spans.append((match.start(), match.end(), entry))
    spans.sort(key=lambda s: (-(s[1] - s[0]), s[0]))
    kept: list[tuple[int, int, dict]] = []
    for start, end, entry in spans:
        if all(end <= k[0] or start >= k[1] for k in kept):
            kept.append((start, end, entry))
    return [entry for _, _, entry in kept]


def split_en(text: str) -> list[str]:
    """句末标点后接空格和大写字母、引号、括号或项目符号时切分。"""
    return [s for s in re.split(r'(?<=[.!?])\s+(?=[A-Z"“(•])', text.strip()) if s]


def split_zh(text: str) -> list[str]:
    return [s.strip() for s in re.split(r"(?<=[。！？])", text.strip()) if s.strip()]


def align(augment: dict, by_id: dict[int, dict]) -> tuple[dict[str, list[dict]], list[str]]:
    aligned: dict[str, list[dict]] = {}
    errors = []
    for key, translation in augment["translations"].items():
        en, zh = split_en(by_id[int(key)]["text"]), split_zh(translation)
        if len(en) != len(zh):
            errors.append(f"块 {key}：原文 {len(en)} 句，译文 {len(zh)} 句，无法逐句对齐")
            continue
        aligned[key] = [{"en": e, "zh": z} for e, z in zip(en, zh, strict=True)]
    print(f"逐句对齐：{len(aligned)}/{len(augment['translations'])} 段")
    return aligned, errors


def check_terms(aligned: dict[str, list[dict]], glossary: list[dict]) -> list[str]:
    errors = []
    total = hit = 0
    for key, pairs in aligned.items():
        for n, pair in enumerate(pairs):
            for entry in {id(e): e for e in term_hits(pair["en"], glossary)}.values():
                total += 1
                if entry["zh"] in pair["zh"]:
                    hit += 1
                else:
                    term, zh = entry["en"][0], entry["zh"]
                    errors.append(f"块 {key} 第 {n + 1} 句：原文有“{term}”，译文缺“{zh}”")
    rate = hit / total if total else 1.0
    print(f"术语命中率（逐句）：{hit}/{total} = {rate:.0%}")
    return errors


def render_pages(pdf: Path, out: Path) -> list[str]:
    pages_dir = out / "pages"
    pages_dir.mkdir(parents=True, exist_ok=True)
    subprocess.run(
        ["pdftoppm", "-r", "120", "-png", str(pdf), str(pages_dir / "p")],
        check=True,
    )
    return [f"pages/{p.name}" for p in sorted(pages_dir.glob("p-*.png"))]


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--content-list", type=Path, required=True)
    parser.add_argument("--pdf", type=Path, required=True)
    parser.add_argument("--out", type=Path, required=True)
    args = parser.parse_args()

    augment = json.loads((HERE / "augment.json").read_text(encoding="utf-8"))
    blocks = load_blocks(args.content_list)
    by_id = {block["i"]: block for block in blocks}

    aligned, align_errors = align(augment, by_id)
    errors = check_quotes(augment, by_id) + align_errors + check_terms(aligned, augment["glossary"])
    if errors:
        print("构建失败：", *errors, sep="\n  ", file=sys.stderr)
        return 1

    if args.out.exists():
        shutil.rmtree(args.out)
    args.out.mkdir(parents=True)
    pages = render_pages(args.pdf, args.out)
    data = {"blocks": blocks, "pages": pages, **augment, "sentences": aligned}
    shutil.copy(args.pdf, args.out / "paper.pdf")
    payload = json.dumps(data, ensure_ascii=False).replace("</", "<\\/")
    (args.out / "data.js").write_text(f"window.READER_DATA = {payload};\n", encoding="utf-8")
    shutil.copy(HERE / "index.html", args.out / "index.html")
    print(f"已生成：{args.out / 'index.html'}（{len(blocks)} 个块，{len(pages)} 页）")
    return 0


if __name__ == "__main__":
    sys.exit(main())
