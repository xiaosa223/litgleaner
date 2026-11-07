// 选中文字：找到所在段落、页码，以及在原版视图里的高亮矩形（页面归一化坐标）。
import { hitBlock } from "./PdfView";
import type { Block, Rect } from "./types";

export interface TextSelection {
  text: string;
  page: number | null;
  block: Block | null;
  rects: Rect[];
  anchor: { x: number; y: number; top: number }; // 工具条的定位
}

const clamp = (v: number) => Math.max(0, Math.min(1000, Math.round(v)));

export function readSelection(root: HTMLElement, blocks: Block[]): TextSelection | null {
  const sel = window.getSelection();
  const text = sel?.toString().replace(/\s+/g, " ").trim();
  if (!sel || !text || !sel.rangeCount || !root.contains(sel.anchorNode)) return null;
  const range = sel.getRangeAt(0);
  const box = range.getBoundingClientRect();
  const anchor = { x: box.left + box.width / 2, y: box.bottom, top: box.top };
  const start = range.startContainer.parentElement;

  const flowBlock = start?.closest<HTMLElement>(".blk[data-b]");
  if (flowBlock) {
    const block = blocks.find((b) => b.index === Number(flowBlock.dataset.b)) ?? null;
    return { text, page: block?.page ?? null, block, rects: [], anchor };
  }
  const pageEl = start?.closest<HTMLElement>(".page");
  if (!pageEl) return null;
  const page = Number(pageEl.dataset.p);
  const pr = pageEl.getBoundingClientRect();
  const rects = [...range.getClientRects()]
    .filter((r) => r.width > 1 && r.height > 1)
    .map((r): Rect => [
      clamp(((r.left - pr.left) / pr.width) * 1000),
      clamp(((r.top - pr.top) / pr.height) * 1000),
      clamp((r.width / pr.width) * 1000),
      clamp((r.height / pr.height) * 1000),
    ])
    .slice(0, 200);
  const first = range.getClientRects()[0] ?? box;
  const block = hitBlock(blocks, page, ((first.left + 2 - pr.left) / pr.width) * 1000, ((first.top + first.height / 2 - pr.top) / pr.height) * 1000) ?? null;
  return { text, page, block, rects, anchor };
}
