// 选中文字：找到所在段落、页码，以及在原版视图里的高亮矩形（页面千分比坐标）。
// 段落视图读浏览器的选区；原版视图读 PDF 引擎按字形算出的选区。
import type { Rect as PdfRect } from "@embedpdf/models";
import type { SelectionScope } from "@embedpdf/plugin-selection";
import { hitBlock } from "./PdfPage";
import type { Block, Rect } from "./types";

export interface TextSelection {
  text: string;
  page: number | null;
  block: Block | null;
  rects: Rect[];
  anchor: { x: number; y: number; top: number }; // 工具条的定位（视口坐标）
  clear: () => void; // 取消选区：段落视图清浏览器选区，原版视图清引擎选区
}

const MAX_RECTS = 200;
const clamp = (v: number) => Math.max(0, Math.min(1000, Math.round(v)));
const clearDom = () => window.getSelection()?.removeAllRanges();

export function readSelection(root: HTMLElement, blocks: Block[]): TextSelection | null {
  const sel = window.getSelection();
  const text = sel?.toString().replace(/\s+/g, " ").trim();
  if (!sel || !text || !sel.rangeCount || !root.contains(sel.anchorNode)) return null;
  const range = sel.getRangeAt(0);
  const box = range.getBoundingClientRect();
  const anchor = { x: box.left + box.width / 2, y: box.bottom, top: box.top };
  const flowBlock = range.startContainer.parentElement?.closest<HTMLElement>(".blk[data-b]");
  if (!flowBlock) return null;
  const block = blocks.find((b) => b.index === Number(flowBlock.dataset.b)) ?? null;
  return { text, page: block?.page ?? null, block, rects: [], anchor, clear: clearDom };
}

/** 原版视图：选区的文字、第一页上的矩形（千分比）、工具条位置。没有选中内容时为 null。 */
export async function pdfSelection(
  scope: SelectionScope, sizes: { width: number; height: number }[], blocks: Block[],
): Promise<TextSelection | null> {
  const parts = scope.getFormattedSelection();
  const first = parts[0];
  const size = first ? sizes[first.pageIndex] : undefined;
  if (!first || !size) return null;
  // 高亮的矩形只记第一页，文字也只取第一页，两者才一致（跨页选区的后半段不保存）
  // 按页分组；PDF 禁止复制时引擎会拒绝，这时不弹工具条
  const pages = await scope.getSelectedText().toPromise().catch((): string[] => []);
  const text = (pages[0] ?? "").replace(/\s+/g, " ").trim();
  if (!text) return null;
  const normalize = (r: PdfRect): Rect => [
    clamp((r.origin.x / size.width) * 1000),
    clamp((r.origin.y / size.height) * 1000),
    clamp((r.size.width / size.width) * 1000),
    clamp((r.size.height / size.height) * 1000),
  ];
  const rects = first.segmentRects.map(normalize).slice(0, MAX_RECTS);
  const [x, y, , h] = normalize(first.segmentRects[0] ?? first.rect);
  const block = hitBlock(blocks, first.pageIndex, x + 2, y + h / 2) ?? null;
  return { text, page: first.pageIndex, block, rects, anchor: anchorOf(first.pageIndex, first.rect, size), clear: () => scope.clear() };
}

function anchorOf(page: number, rect: PdfRect, size: { width: number }): TextSelection["anchor"] {
  const element = document.querySelector<HTMLElement>(`.page[data-p="${page}"]`);
  const box = element?.getBoundingClientRect();
  if (!box) return { x: window.innerWidth / 2, y: window.innerHeight / 2, top: window.innerHeight / 2 };
  const k = box.width / size.width;
  return {
    x: box.left + (rect.origin.x + rect.size.width / 2) * k,
    y: box.top + (rect.origin.y + rect.size.height) * k,
    top: box.top + rect.origin.y * k,
  };
}
