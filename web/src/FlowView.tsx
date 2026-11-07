// 段落视图：由统一文档重排。配图按 bbox 从 PDF 裁出，表格只保留表格结构，公式用 KaTeX。
import { useEffect, useRef, useState } from "react";
import type { PdfState } from "./pdf";
import type { BlockAction, Focus } from "./PdfView";
import { headingLevel, isReferencesHeading, rich, safeTable, tex } from "./text";
import type { Annotation, Block, DocumentView, PaperView } from "./types";
import { SOON } from "./ui";

const FIGURE_WIDTH = 560;
const ABSTRACT_MARK = /^(abstract\b|摘\s*要)/i;
const FIGURE_HEIGHT = 520; // 竖长的图按高度限制，不占满一屏

function FigureCrop({ pdf, block }: { pdf: PdfState; block: Block }) {
  const host = useRef<HTMLDivElement>(null);
  const [x0, y0, x1, y1] = block.bbox!;
  const size = pdf.sizes[block.page];
  const cropWidth = ((x1 - x0) / 1000) * size.width;
  const cropHeight = ((y1 - y0) / 1000) * size.height;
  const scale = Math.min(FIGURE_WIDTH / Math.max(cropWidth, 1), FIGURE_HEIGHT / Math.max(cropHeight, 1), 3);

  useEffect(() => {
    let cancelled = false;
    const target = host.current;
    (async () => {
      const page = await pdf.doc.getPage(block.page + 1);
      const viewport = page.getViewport({ scale });
      const ratio = window.devicePixelRatio || 1;
      const canvas = document.createElement("canvas");
      canvas.width = Math.floor(viewport.width * ratio);
      canvas.height = Math.floor(viewport.height * ratio);
      Object.assign(canvas.style, {
        position: "absolute",
        width: `${viewport.width}px`,
        left: `${(-x0 / 1000) * viewport.width}px`,
        top: `${(-y0 / 1000) * viewport.height}px`,
      });
      await page.render({ canvas, viewport, transform: ratio === 1 ? undefined : [ratio, 0, 0, ratio, 0, 0] }).promise;
      if (!cancelled && target) target.replaceChildren(canvas);
    })().catch(() => undefined); // 裁图失败时只显示图注
    return () => {
      cancelled = true;
    };
  }, [pdf, block.page, scale, x0, y0]);

  return (
    <div className="crop" ref={host}
      style={{ position: "relative", overflow: "hidden", width: cropWidth * scale, maxWidth: "100%", height: cropHeight * scale }} />
  );
}

function BlockView(props: {
  block: Block; pdf: PdfState | null; marks: { quote: string; cls: string }[]; focused: boolean;
  onAction: (action: BlockAction, block: Block) => void; onToPdf: (block: Block) => void;
}) {
  const { block: b, pdf, marks, focused, onAction, onToPdf } = props;
  const id = `b-${b.index}`;
  if (b.kind === "heading") {
    const level = headingLevel(b.text);
    const Tag = `h${level}` as "h2";
    return <Tag id={id} className="hd" data-page={b.page}>{b.text}</Tag>;
  }
  if (b.kind === "figure") {
    return (
      <figure className="fig" id={id}>
        {pdf && b.bbox && <FigureCrop pdf={pdf} block={b} />}
        <figcaption>{rich(b.caption ?? b.text)}</figcaption>
      </figure>
    );
  }
  if (b.kind === "table") {
    return (
      <figure className="tbl" id={id}>
        {b.caption && <figcaption>{rich(b.caption)}</figcaption>}
        {safeTable(b.text)}
      </figure>
    );
  }
  if (b.kind === "equation") return <div className="eq" id={id}>{tex(b.text.replace(/^\s*\$\$|\$\$\s*$/g, ""), true)}</div>;
  if (b.kind === "code") return <pre id={id} style={{ font: "13px monospace", whiteSpace: "pre-wrap" }}>{b.text}</pre>;
  if (!b.text.trim()) {
    return (
      <div className="blk" id={id}>
        <div className="gap-note">此处有内容未能解析（第 {b.page + 1} 页）。<button onClick={() => onToPdf(b)}>在原版中查看</button></div>
      </div>
    );
  }
  return (
    <div className={`blk${focused ? " focus" : ""}`} id={id} data-b={b.index} data-page={b.page}>
      <div className="side">
        <button disabled title={SOON.translate}>译</button>
        <button onClick={() => onAction("ask", b)}>问</button>
        <button onClick={() => onAction("note", b)}>记</button>
      </div>
      <div className="en">{rich(b.text, marks)}</div>
    </div>
  );
}

export function FlowView(props: {
  paper: PaperView; document: DocumentView; pdf: PdfState | null; annotations: Annotation[];
  focus: (Focus & { quote: string | null }) | null; scroller: HTMLElement | null;
  onAction: (action: BlockAction, block: Block) => void; onToPdf: (block: Block) => void;
}) {
  const { paper, document: doc, pdf, annotations, focus, scroller, onAction, onToPdf } = props;
  const [flash, setFlash] = useState<{ block: number; quote: string | null } | null>(null);

  useEffect(() => {
    if (!focus || !scroller || focus.block === null) return;
    const target = document.getElementById(`b-${focus.block}`);
    if (!target) return;
    const r = target.getBoundingClientRect(), d = scroller.getBoundingClientRect();
    scroller.scrollTo({ top: scroller.scrollTop + r.top - d.top - (scroller.clientHeight - Math.min(r.height, scroller.clientHeight)) / 2, behavior: "smooth" });
    setFlash({ block: focus.block, quote: focus.quote });
    const timer = window.setTimeout(() => setFlash(null), 1800);
    return () => window.clearTimeout(timer);
  }, [focus, scroller]);

  const highlights = annotations.filter((a) => a.kind === "highlight" && a.quote);
  // 从第一个摘要标记开始（之前是标题、作者、单位，已在顶部显示）；中文期刊的中文摘要在英文标题之前，
  // 所以不能按“第一个标题”截。找不到摘要标记时从头显示，宁可多显示也不丢内容。到参考文献为止。
  const first = Math.max(0, doc.blocks.findIndex((b) => ABSTRACT_MARK.test(b.text.trim())));
  const end = doc.blocks.findIndex((b) => b.kind === "heading" && isReferencesHeading(b.text));
  const blocks = doc.blocks.slice(first, end >= 0 ? end + 1 : undefined).filter((b) => b.kind !== "reference");
  const gaps = blocks.filter((b) => b.kind === "text" && !b.text.trim()).length;
  const meta = [paper.authors.slice(0, 6).join(", "), paper.venue, paper.published_at].filter(Boolean).join(" · ");

  return (
    <article className="flow">
      <h1>{paper.title}</h1>
      <div className="meta">{meta}</div>
      {(doc.quality === "degraded" || gaps > 0) && (
        <div className="degraded">
          结构识别不完整{gaps > 0 ? `：有 ${gaps} 处内容没有解析出来（多为公式或表格），已在原位标出` : ""}，以原版视图为准。
        </div>
      )}
      {blocks.map((b) => {
        const marks = highlights.filter((a) => a.page === b.page).map((a) => ({ quote: a.quote!, cls: "hl hl-user" }));
        if (flash?.block === b.index && flash.quote) marks.unshift({ quote: flash.quote, cls: "flash" });
        return <BlockView key={b.index} block={b} pdf={pdf} marks={marks} focused={flash?.block === b.index}
          onAction={onAction} onToPdf={onToPdf} />;
      })}
    </article>
  );
}
