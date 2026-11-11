// 段落视图：由统一文档重排。配图按 bbox 从 PDF 裁出，表格只保留表格结构，公式用 KaTeX。
import { useEffect, useState, type ReactNode } from "react";
import type { PdfState } from "./pdf";
import type { BlockAction, Focus } from "./PdfView";
import { headingLevel, isReferencesHeading, rich, safeTable, tex } from "./text";
import type { Translations } from "./translations";
import { EnText, ZhText, type Pairing } from "./TranslationView";
import type { Annotation, Block, DocumentView, PaperView } from "./types";

export interface FlowTranslation {
  translations: Translations;
  bilingual: boolean; // 双栏对照
  overlay: boolean; // 全文覆盖，只在原版视图
  open: Set<number>; // 单段展开译文的段落
  onToggle: (index: number) => void;
  pairing: Pairing;
}

const FIGURE_WIDTH = 560;
const FIGURE_HEIGHT = 520; // 竖长的图按高度限制，不占满一屏

function FigureCrop({ pdf, block }: { pdf: PdfState; block: Block }) {
  const [src, setSrc] = useState<string | null>(null);
  const [x0, y0, x1, y1] = block.bbox!;
  const size = pdf.sizes[block.page];
  const cropWidth = ((x1 - x0) / 1000) * size.width;
  const cropHeight = ((y1 - y0) / 1000) * size.height;
  const scale = Math.min(FIGURE_WIDTH / Math.max(cropWidth, 1), FIGURE_HEIGHT / Math.max(cropHeight, 1), 3);

  useEffect(() => {
    const page = pdf.doc.pages[block.page];
    if (!page) return;
    let url: string | null = null;
    let cancelled = false;
    const rect = { origin: { x: (x0 / 1000) * size.width, y: (y0 / 1000) * size.height }, size: { width: cropWidth, height: cropHeight } };
    pdf.engine
      .renderPageRect(pdf.doc, page, rect, { scaleFactor: scale, dpr: window.devicePixelRatio || 1, imageType: "image/png" })
      .toPromise()
      .then((blob) => {
        if (cancelled) return;
        url = URL.createObjectURL(blob);
        setSrc(url);
      }, () => undefined); // 裁图失败时只显示图注
    return () => {
      cancelled = true;
      if (url) URL.revokeObjectURL(url);
    };
  }, [pdf, block.page, scale, x0, y0, cropWidth, cropHeight, size.width, size.height]);

  return (
    <div className="crop" style={{ position: "relative", overflow: "hidden", width: cropWidth * scale, maxWidth: "100%", height: cropHeight * scale }}>
      {src && <img src={src} alt="" style={{ width: "100%", height: "100%", display: "block" }} />}
    </div>
  );
}

function BlockView(props: {
  block: Block; pdf: PdfState | null; marks: { quote: string; cls: string }[]; focused: boolean;
  onAction: (action: BlockAction, block: Block) => void; onToPdf: (block: Block) => void; tr: FlowTranslation; canAsk: boolean;
}) {
  const { block: b, pdf, marks, focused, onAction, onToPdf, tr, canAsk } = props;
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
  const done = tr.translations.done.get(b.index);
  const en = <EnText index={b.index} text={b.text} tr={done} marks={marks} pairing={tr.pairing} />;
  const zh = (idle: ReactNode) => (
    <ZhText index={b.index} translations={tr.translations} pairing={tr.pairing} idle={idle} quiet={tr.bilingual} />
  );
  const side = (
    <div className="side">
      {!tr.bilingual && <button disabled={!b.translatable} title={b.translatable ? "翻译这段" : "这段不需要翻译"} onClick={() => tr.onToggle(b.index)}>译</button>}
      {canAsk && <button onClick={() => onAction("ask", b)}>问</button>}
      <button onClick={() => onAction("note", b)}>记</button>
    </div>
  );
  if (tr.bilingual) {
    const idle = b.translatable
      ? <button className="ghost" onClick={() => tr.translations.translate(b.index)}>翻译这段</button>
      : <span className="tag">原文即可，不需要翻译</span>;
    const hasZh = done || tr.translations.live.has(b.index) || tr.translations.errors.has(b.index);
    return (
      <div className={`blk${focused ? " focus" : ""}`} id={id} data-b={b.index} data-page={b.page}>
        {side}
        <div className="bi-row"><div className="en">{en}</div><div className={`zhcol${hasZh ? "" : " missing"}`}>{zh(idle)}</div></div>
      </div>
    );
  }
  return (
    <div className={`blk${focused ? " focus" : ""}`} id={id} data-b={b.index} data-page={b.page}>
      {side}
      <div className="en">{en}</div>
      {tr.open.has(b.index) && <div className="zh-inline">{zh("正在翻译…")}</div>}
    </div>
  );
}

export function FlowView(props: {
  paper: PaperView; document: DocumentView; pdf: PdfState | null; annotations: Annotation[];
  focus: (Focus & { quote: string | null }) | null; scroller: HTMLElement | null;
  onAction: (action: BlockAction, block: Block) => void; onToPdf: (block: Block) => void; translation: FlowTranslation; canAsk: boolean;
}) {
  const { paper, document: doc, pdf, annotations, focus, scroller, onAction, onToPdf, translation, canAsk } = props;
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
  // 从正文开始处（服务端按第一个摘要标记算出）显示到参考文献为止；之前的标题、作者已在顶部显示
  const first = doc.body_start;
  const end = doc.blocks.findIndex((b) => b.kind === "heading" && isReferencesHeading(b.text));
  const blocks = doc.blocks.slice(first, end >= 0 ? end + 1 : undefined).filter((b) => b.kind !== "reference");
  const gaps = blocks.filter((b) => b.kind === "text" && !b.text.trim()).length;
  const meta = [paper.authors.slice(0, 6).join(", "), paper.venue, paper.published_at].filter(Boolean).join(" · ");

  return (
    <article className={`flow${translation.bilingual ? " bi" : ""}`}>
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
          onAction={onAction} onToPdf={onToPdf} tr={translation} canAsk={canAsk} />;
      })}
    </article>
  );
}
