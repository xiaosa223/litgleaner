// 原版视图：pdf.js 渲染页面，叠加文字层（可选中原文）、段落区域（悬停出现“译 / 问 / 记”）和用户高亮。
import { TextLayer } from "pdfjs-dist";
import "pdfjs-dist/web/pdf_viewer.css";
import { useEffect, useRef, useState, type MouseEvent } from "react";
import type { PdfState } from "./pdf";
import type { Annotation, Block } from "./types";
import { SOON } from "./ui";

export type BlockAction = "ask" | "note";
export type Focus = { block: number | null; page: number; nonce: number };

const MAX_PAGE_WIDTH = 900;
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

function Page(props: {
  pdf: PdfState; index: number; width: number; blocks: Block[]; annotations: Annotation[];
  hovered: number | null; focused: number | null; onAction: (action: BlockAction, block: Block) => void;
}) {
  const { pdf, index, width, blocks, annotations, hovered, focused, onAction } = props;
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
          const cls = ["ov", hovered === b.index ? "hov" : "", focused === b.index ? "focus" : ""].join(" ");
          return (
            <div key={b.index} id={`o-${b.index}`} className={cls} style={{ left: pct(x0), top: pct(y0), width: pct(x1 - x0), height: pct(y1 - y0) }}>
              {isBody(b) && (
                <div className="acts">
                  <button disabled title={SOON.translate}>译</button>
                  <button onClick={() => onAction("ask", b)}>问</button>
                  <button onClick={() => onAction("note", b)}>记</button>
                </div>
              )}
            </div>
          );
        })}
        <span className="no">第 {index + 1} 页</span>
      </div>
    </div>
  );
}

export function PdfView(props: {
  pdf: PdfState; blocks: Block[]; annotations: Annotation[]; focus: Focus | null;
  scroller: HTMLElement | null; onAction: (action: BlockAction, block: Block) => void;
}) {
  const { pdf, blocks, annotations, focus, scroller, onAction } = props;
  const [width, setWidth] = useState(0);
  const [hovered, setHovered] = useState<number | null>(null);
  const [focused, setFocused] = useState<number | null>(null);

  useEffect(() => {
    if (!scroller) return;
    const measure = () => setWidth(Math.min(scroller.clientWidth - 32, MAX_PAGE_WIDTH));
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

  if (width <= 0) return null;
  return (
    <div className="pages" onMouseMove={onMove} onMouseLeave={() => setHovered(null)}>
      {pdf.sizes.map((_, index) => (
        <Page key={index} pdf={pdf} index={index} width={width} blocks={blocks} annotations={annotations}
          hovered={hovered} focused={focused} onAction={onAction} />
      ))}
    </div>
  );
}
