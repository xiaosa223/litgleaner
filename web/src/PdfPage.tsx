// 原版视图的一页：PDFium 渲染（低清底图 + 分块高清），字形选字层，再叠加段落区域、用户高亮和双语对照的译文栏。
// 叠加物都按页面千分比坐标定位，和页面实际尺寸无关。
import { PagePointerProvider } from "@embedpdf/plugin-interaction-manager/react";
import { RenderLayer } from "@embedpdf/plugin-render/react";
import { SelectionLayer } from "@embedpdf/plugin-selection/react";
import { TilingLayer } from "@embedpdf/plugin-tiling/react";
import { useLayoutEffect, useRef } from "react";
import type { FlowTranslation } from "./FlowView";
import { ZhText } from "./TranslationView";
import type { Annotation, Block } from "./types";

export type BlockAction = "ask" | "note";
export type Focus = { block: number | null; page: number; nonce: number };

export const DOC_ID = "paper";
export const COLUMN_GAP = 20;
const pct = (v: number) => `${(v / 10).toFixed(2)}%`;
export const isBody = (b: Block) => b.kind === "text" && b.bbox !== null;

/** 鼠标所在的正文段落。右侧留出按钮区，移到按钮上不会丢失悬停。 */
export function hitBlock(blocks: Block[], page: number, x: number, y: number): Block | undefined {
  return blocks.find((b) => {
    if (b.page !== page || !isBody(b) || !b.bbox) return false;
    const [x0, y0, x1, y1] = b.bbox;
    return x >= x0 - 10 && x <= x1 + 90 && y >= y0 - 6 && y <= y1 + 6;
  });
}

/** 双语对照：译文按段落在页面上的高度排在右侧，重叠时依次下移。 */
function TransColumn({ blocks, height, width, left, tr, onHover }: {
  blocks: Block[]; height: number; width: number; left: number; tr: FlowTranslation; onHover: (index: number | null) => void;
}) {
  const column = useRef<HTMLDivElement>(null);
  useLayoutEffect(() => {
    // 顶部的“翻译本页”按钮占住的高度不能被译文盖住
    let bottom = column.current?.querySelector<HTMLElement>(".trans-page")?.offsetHeight ?? -10;
    column.current?.querySelectorAll<HTMLElement>(".tb").forEach((element) => {
      const top = Math.max((Number(element.dataset.y) / 1000) * height, bottom + 10);
      element.style.top = `${top}px`;
      bottom = top + element.offsetHeight;
    });
    if (column.current) column.current.style.minHeight = `${Math.max(height, bottom)}px`;
  });
  const t = tr.translations;
  const shown = blocks.filter((b) => t.done.has(b.index) || t.live.has(b.index) || t.errors.has(b.index));
  const pending = blocks.filter((b) => !t.done.has(b.index) && !t.live.has(b.index)).map((b) => b.index);
  return (
    <div className="trans-col" ref={column} style={{ position: "absolute", top: 0, left, width }}>
      {pending.length > 0 && (
        <button className="ghost trans-page" onClick={() => t.translateAll(pending)} title="按段落分批翻译，会产生模型费用">
          翻译本页 {pending.length} 段
        </button>
      )}
      {shown.map((b) => (
        <div key={b.index} className="tb" data-y={b.bbox![1]} onMouseEnter={() => onHover(b.index)} onMouseLeave={() => onHover(null)}>
          <ZhText index={b.index} translations={t} pairing={tr.pairing} idle={null} quiet />
        </div>
      ))}
    </div>
  );
}

export function PdfPage(props: {
  index: number; width: number; height: number; blocks: Block[]; annotations: Annotation[];
  hovered: number | null; focused: number | null; onAction: (action: BlockAction, block: Block) => void;
  tr: FlowTranslation; columnWidth: number; onHover: (index: number | null) => void; canAsk: boolean;
}) {
  const { index, width, height, blocks, annotations, hovered, focused, onAction, tr, columnWidth, onHover, canAsk } = props;
  const rects = annotations.filter((a) => a.page === index).flatMap((a) => a.rects.map((r, k) => ({ r, key: `${a.id}-${k}` })));
  return (
    <div className="page" data-p={index} style={{ width, height }}>
      {/* 指针事件交给 PDF 引擎：按字形选字 */}
      <PagePointerProvider documentId={DOC_ID} pageIndex={index} style={{ position: "absolute", inset: 0 }}>
        <RenderLayer documentId={DOC_ID} pageIndex={index} scale={1} className="pdf-base" />
        <TilingLayer documentId={DOC_ID} pageIndex={index} style={{ position: "absolute", inset: 0 }} />
        <SelectionLayer documentId={DOC_ID} pageIndex={index} />
      </PagePointerProvider>
      {rects.map(({ r, key }) => (
        <div key={key} className="uhl" style={{ left: pct(r[0]), top: pct(r[1]), width: pct(r[2]), height: pct(r[3]) }} />
      ))}
      {blocks.filter((b) => b.page === index && b.bbox && b.kind !== "reference").map((b) => {
        const [x0, y0, x1, y1] = b.bbox!;
        const pop = tr.open.has(b.index) && !tr.bilingual;
        const cls = ["ov", hovered === b.index ? "hov" : "", focused === b.index ? "focus" : "", pop ? "has-pop" : ""].join(" ");
        return (
          <div key={b.index} id={`o-${b.index}`} className={cls} style={{ left: pct(x0), top: pct(y0), width: pct(x1 - x0), height: pct(y1 - y0) }}>
            {isBody(b) && (
              <div className="acts">
                {!tr.bilingual && <button disabled={!b.translatable} title={b.translatable ? "翻译这段" : "这段不需要翻译"} onClick={() => tr.onToggle(b.index)}>译</button>}
                {canAsk && <button onClick={() => onAction("ask", b)}>问</button>}
                <button onClick={() => onAction("note", b)}>记</button>
              </div>
            )}
            {pop && (
              <div className="pop" style={{ top: "calc(100% + 8px)" }}>
                <button className="x" aria-label="关闭" onClick={() => tr.onToggle(b.index)}>×</button>
                <div style={{ fontSize: 14.5, lineHeight: 1.75 }}>
                  <ZhText index={b.index} translations={tr.translations} pairing={tr.pairing} idle="正在翻译…" />
                </div>
              </div>
            )}
          </div>
        );
      })}
      <span className="no">第 {index + 1} 页</span>
      {tr.bilingual && (
        <TransColumn blocks={blocks.filter((b) => b.page === index && b.translatable && b.bbox)} height={height}
          width={columnWidth} left={width + COLUMN_GAP} tr={tr} onHover={onHover} />
      )}
    </div>
  );
}
