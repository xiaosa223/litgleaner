// 原版视图：pdf.js 渲染页面，叠加文字层（可选中原文）、段落区域（悬停出现“译 / 问 / 记”）和用户高亮。
import { TextLayer } from "pdfjs-dist";
import "pdfjs-dist/web/pdf_viewer.css";
import { useEffect, useLayoutEffect, useRef, useState, type MouseEvent } from "react";
import type { FlowTranslation } from "./FlowView";
import type { PdfState } from "./pdf";
import { ZhText } from "./TranslationView";
import type { Annotation, Block } from "./types";

export type BlockAction = "ask" | "note";
export type Focus = { block: number | null; page: number; nonce: number };

const MAX_PAGE_WIDTH = 900;
const MAX_ROW_WIDTH = 1240; // 双语对照时页面加右侧译文栏
const COLUMN_GAP = 20;
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
function TransColumn({ blocks, height, width, tr, onHover }: {
  blocks: Block[]; height: number; width: number; tr: FlowTranslation; onHover: (index: number | null) => void;
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
    if (column.current) column.current.style.height = `${Math.max(height, bottom)}px`;
  });
  const t = tr.translations;
  const shown = blocks.filter((b) => t.done.has(b.index) || t.live.has(b.index) || t.errors.has(b.index));
  const pending = blocks.filter((b) => !t.done.has(b.index) && !t.live.has(b.index)).map((b) => b.index);
  return (
    <div className="trans-col" ref={column} style={{ flex: `0 0 ${width}px` }}>
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

function Page(props: {
  pdf: PdfState; index: number; width: number; blocks: Block[]; annotations: Annotation[];
  hovered: number | null; focused: number | null; onAction: (action: BlockAction, block: Block) => void;
  tr: FlowTranslation; columnWidth: number; onHover: (index: number | null) => void;
}) {
  const { pdf, index, width, blocks, annotations, hovered, focused, onAction, tr, columnWidth, onHover } = props;
  const size = pdf.sizes[index];
  const height = (width / size.width) * size.height;
  const host = useRef<HTMLDivElement>(null);
  const layer = useRef<HTMLDivElement>(null);
  const [visible, setVisible] = useState(false);

  useEffect(() => {
    const element = host.current;
    if (!element) return;
    const observer = new IntersectionObserver(([e]) => e.isIntersecting && setVisible(true), { rootMargin: "1200px 0px" });
    observer.observe(element);
    return () => observer.disconnect();
  }, []);

  useEffect(() => {
    if (!visible || !layer.current) return;
    let cancelled = false;
    const target = layer.current;
    (async () => {
      const page = await pdf.doc.getPage(index + 1);
      const viewport = page.getViewport({ scale: width / size.width });
      const ratio = window.devicePixelRatio || 1;
      const canvas = document.createElement("canvas");
      canvas.className = "pdf-canvas";
      canvas.width = Math.floor(viewport.width * ratio);
      canvas.height = Math.floor(viewport.height * ratio);
      const text = document.createElement("div");
      text.className = "textLayer";
      text.style.setProperty("--scale-factor", String(viewport.scale));
      await page.render({ canvas, viewport, transform: ratio === 1 ? undefined : [ratio, 0, 0, ratio, 0, 0] }).promise;
      if (cancelled) return;
      target.replaceChildren(canvas, text);
      await new TextLayer({ textContentSource: page.streamTextContent(), container: text, viewport }).render();
    })().catch((error: unknown) => {
      if (!cancelled) target.textContent = `第 ${index + 1} 页渲染失败：${String(error)}`;
    });
    return () => {
      cancelled = true;
    };
  }, [visible, pdf, index, width, size.width]);

  const rects = annotations.filter((a) => a.page === index).flatMap((a) => a.rects.map((r, k) => ({ r, key: `${a.id}-${k}` })));
  return (
    <div className="page-row">
      <div className="page" ref={host} data-p={index} style={{ width, height }}>
        <div ref={layer} style={{ position: "absolute", inset: 0 }} />
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
                  <button onClick={() => onAction("ask", b)}>问</button>
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
      </div>
      {tr.bilingual && (
        <TransColumn blocks={blocks.filter((b) => b.page === index && b.translatable && b.bbox)} height={height}
          width={columnWidth} tr={tr} onHover={onHover} />
      )}
    </div>
  );
}

export function PdfView(props: {
  pdf: PdfState; blocks: Block[]; annotations: Annotation[]; focus: Focus | null;
  scroller: HTMLElement | null; onAction: (action: BlockAction, block: Block) => void; translation: FlowTranslation;
}) {
  const { pdf, blocks, annotations, focus, scroller, onAction, translation } = props;
  const [available, setAvailable] = useState(0);
  const [hovered, setHovered] = useState<number | null>(null);
  const [focused, setFocused] = useState<number | null>(null);

  useEffect(() => {
    if (!scroller) return;
    const measure = () => setAvailable(scroller.clientWidth - 32);
    measure();
    const observer = new ResizeObserver(measure);
    observer.observe(scroller);
    return () => observer.disconnect();
  }, [scroller]);

  useEffect(() => {
    if (!focus || !scroller) return;
    const target = (focus.block !== null && document.getElementById(`o-${focus.block}`)) || scroller.querySelector(`.page[data-p="${focus.page}"]`);
    if (!(target instanceof HTMLElement)) return;
    const r = target.getBoundingClientRect(), d = scroller.getBoundingClientRect();
    scroller.scrollTo({ top: scroller.scrollTop + r.top - d.top - (scroller.clientHeight - Math.min(r.height, scroller.clientHeight)) / 2, behavior: "smooth" });
    setFocused(focus.block);
    const timer = window.setTimeout(() => setFocused(null), 1800);
    return () => window.clearTimeout(timer);
  }, [focus, scroller]);

  const onMove = (event: MouseEvent) => {
    const page = (event.target as HTMLElement).closest<HTMLElement>(".page");
    if (!page) return setHovered(null);
    const r = page.getBoundingClientRect();
    const hit = hitBlock(blocks, Number(page.dataset.p), ((event.clientX - r.left) / r.width) * 1000, ((event.clientY - r.top) / r.height) * 1000);
    setHovered(hit?.index ?? null);
  };

  const row = Math.min(available, translation.bilingual ? MAX_ROW_WIDTH : MAX_PAGE_WIDTH);
  const width = translation.bilingual ? Math.floor((row - COLUMN_GAP) * 0.6) : row;
  const columnWidth = row - width - COLUMN_GAP;
  if (width <= 0) return null;
  return (
    <div className="pages" onMouseMove={onMove} onMouseLeave={() => setHovered(null)}>
      {pdf.sizes.map((_, index) => (
        <Page key={index} pdf={pdf} index={index} width={width} blocks={blocks} annotations={annotations}
          hovered={hovered} focused={focused} onAction={onAction} tr={translation} columnWidth={columnWidth} onHover={setHovered} />
      ))}
    </div>
  );
}
