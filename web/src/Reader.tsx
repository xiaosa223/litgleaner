// 阅读页：顶栏 + 目录 | 正文（原版或段落视图）| 侧栏。布局和交互沿用设计研究的演示。
import { useCallback, useEffect, useState } from "react";
import { api } from "./api";
import { FlowView, type FlowTranslation } from "./FlowView";
import { Panel, type Tab } from "./panel/Panel";
import { usePdf } from "./pdf";
import { PdfView, type BlockAction, type Focus } from "./PdfView";
import { usePref } from "./prefs";
import { ReaderHeader, type View } from "./ReaderHeader";
import { SelectionBar, NoteDialog, type NoteTarget } from "./SelectionBar";
import { readSelection, type TextSelection } from "./selection";
import { SelectionPopup } from "./SelectionPopup";
import { useTranslations } from "./translations";
import { Toc } from "./Toc";
import type { Annotation, Block } from "./types";
import { Modal, SOON, useToast } from "./ui";
import { useAsync } from "./useAsync";
import { currentPage, useReadingPosition } from "./useReadingPosition";

const HELP = `选中文字   翻译 / 高亮 / 记笔记 / 提问
1 / 2      原版 / 段落视图
T          原文 ↔ 双语对照
/          切到问答
Esc        关闭浮层；再按回到纯原文
?          快捷键`;

function locate(blocks: Block[], note: Annotation): Block | null {
  const same = (b: Block) => b.page === note.page && note.bbox !== null && b.bbox !== null && b.bbox.every((v, i) => v === note.bbox![i]);
  const quoted = (b: Block) => b.page === note.page && !!note.quote && b.text.replace(/\s+/g, " ").includes(note.quote);
  return blocks.find(same) ?? blocks.find(quoted) ?? null;
}

export function Reader({ paperId }: { paperId: string }) {
  const toast = useToast();
  const [paperState, reload] = useAsync(() => api.paper(paperId), [paperId]);
  const paper = paperState.state === "ready" ? paperState.data : null;
  const [docState] = useAsync(() => (paper?.has_document ? api.document(paperId) : Promise.resolve(null)), [paperId, paper?.has_document]);
  const doc = docState.state === "ready" ? docState.data : null;
  const { pdf, error: pdfError } = usePdf(paper?.has_pdf ? api.pdfUrl(paperId) : null);
  const [costState, reloadCosts] = useAsync(() => api.costs(paperId), [paperId]);
  const [preferred, setPreferred] = usePref<View>("view", "pdf");
  const [tab, setTab] = useState<Tab>("guide");
  const [scroller, setScroller] = useState<HTMLElement | null>(null);
  const [focus, setFocus] = useState<(Focus & { quote: string | null }) | null>(null);
  const [selection, setSelection] = useState<TextSelection | null>(null);
  const [noteTarget, setNoteTarget] = useState<NoteTarget | null>(null);
  const [autoHl, setAutoHl] = useState(false);
  const [help, setHelp] = useState(false);
  const [activeHeading, setActiveHeading] = useState<number | null>(null);
  const [restored, setRestored] = useState(false);
  const [bilingual, setBilingual] = usePref<boolean>("bilingual", false);
  const [open, setOpen] = useState(new Set<number>());
  const [hoverSentence, setHoverSentence] = useState<string | null>(null);
  const [popup, setPopup] = useState<TextSelection | null>(null);

  const blocks = doc?.blocks ?? [];
  const translations = useTranslations(paperId, !!doc, reloadCosts);
  const canTranslate = blocks.some((b) => b.translatable);
  const showBilingual = bilingual && canTranslate;
  const toggleOpen = (index: number) => {
    const next = new Set(open);
    if (next.has(index)) next.delete(index);
    else {
      next.add(index);
      if (!translations.done.has(index)) translations.translate(index);
    }
    setOpen(next);
  };
  const translation: FlowTranslation = {
    translations, bilingual: showBilingual, open, onToggle: toggleOpen,
    pairing: { hover: hoverSentence, onHover: setHoverSentence },
  };
  const canPdf = !!paper?.has_pdf && !pdfError, canFlow = !!doc;
  const view: View = preferred === "flow" && canFlow ? "flow" : canPdf ? "pdf" : canFlow ? "flow" : "pdf";
  const savePosition = useReadingPosition(paper, view, reload);

  const jumpTo = useCallback((block: Block | null, page: number, quote: string | null = null) => {
    if (view === "flow" && !block) block = blocks.find((b) => b.page >= page) ?? null;
    setFocus({ block: block?.index ?? null, page, quote, nonce: Date.now() });
  }, [view, blocks]);

  // 打开时回到上次读到的位置
  useEffect(() => {
    if (restored || !paper || !scroller || (view === "pdf" ? !pdf : !doc)) return;
    setRestored(true);
    const page = paper.reading?.position?.page;
    if (typeof page === "number" && page > 0) jumpTo(null, page);
  }, [restored, paper, scroller, view, pdf, doc, jumpTo]);

  const create = async (kind: "highlight" | "note", target: NoteTarget, body: string | null) => {
    try {
      await api.addAnnotation(paperId, {
        kind, body, quote: target.quote, page: target.page, bbox: target.block?.bbox ?? null, rects: kind === "highlight" ? target.rects : [],
      });
      toast(kind === "highlight" ? "已高亮" : "已记到笔记");
      reload();
    } catch (error) {
      toast(error instanceof Error ? error.message : "保存失败");
    }
  };

  const onSelectionAction = (action: "translate" | "highlight" | "note" | "ask" | "auto", sel: TextSelection) => {
    setSelection(null);
    if (action === "translate") { setPopup(sel); return; }
    const target: NoteTarget = { quote: sel.text, page: sel.page, block: sel.block, rects: sel.rects };
    if (action === "ask") { setTab("qa"); toast(SOON.ask); return; }
    if (action === "note") { setNoteTarget(target); return; }
    if (action === "auto") { setAutoHl(true); toast("连续高亮已开启：选中即高亮，按 Esc 关闭"); }
    sel.clear();
    void create("highlight", target, null);
  };

  const onBlockAction = (action: BlockAction, block: Block) => {
    if (action === "ask") { setTab("qa"); toast(SOON.ask); return; }
    setNoteTarget({ quote: null, page: block.page, block, rects: [] });
  };

  const onSelected = (sel: TextSelection | null) => {
    setPopup(null);
    if (!sel) return setSelection(null);
    if (autoHl) { sel.clear(); return void create("highlight", { quote: sel.text, page: sel.page, block: sel.block, rects: sel.rects }, null); }
    setSelection(sel);
  };

  // 段落视图读浏览器选区；原版视图的选区由 PDF 引擎回调 onSelected
  const onMouseUp = (event: React.MouseEvent) => {
    if (view !== "flow" || !scroller || (event.target as HTMLElement).closest(".acts, .side, button")) return;
    window.setTimeout(() => onSelected(readSelection(scroller, blocks)), 0);
  };

  const onScroll = () => {
    setSelection(null);
    if (!scroller || view !== "flow") return; // 原版视图自己滚动，页码由 PdfView 回调
    const page = currentPage(scroller, "[data-page]", "page");
    if (page !== null) savePosition(page);
    const headings = [...scroller.querySelectorAll<HTMLElement>(".flow .hd")].filter((h) => h.getBoundingClientRect().top < 160);
    const last = headings.pop();
    setActiveHeading(last ? Number(last.id.slice(2)) : null);
  };

  useEffect(() => {
    const onKey = (event: KeyboardEvent) => {
      if ((event.target as HTMLElement).matches("textarea, input")) return;
      if (event.key === "1" && canPdf) setPreferred("pdf");
      if (event.key === "2" && canFlow) setPreferred("flow");
      if (event.key === "/") { event.preventDefault(); setTab("qa"); }
      if ((event.key === "t" || event.key === "T") && canTranslate) setBilingual(!bilingual);
      if (event.key === "?") setHelp(true);
      if (event.key === "Escape") {
        if (help || noteTarget) { setHelp(false); setNoteTarget(null); return; }
        if (selection || popup) { setSelection(null); setPopup(null); return; }
        if (autoHl) { setAutoHl(false); toast("已关闭连续高亮"); return; }
        pure();
      }
    };
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, [canPdf, canFlow, setPreferred, help, noteTarget, selection, popup, autoHl, toast, canTranslate, bilingual, setBilingual]);

  if (paperState.state === "loading") return <div className="view-msg">正在加载…</div>;
  if (paperState.state === "error") return <div className="view-msg">{paperState.message}</div>;
  const data = paperState.data;
  function pure() {
    setBilingual(false);
    setOpen(new Set());
    toast("已回到纯原文，可以随时重新打开各项增强");
  }
  const markRead = () => void api.setReading(paperId, "read").then(() => { toast("已标记为已读"); reload(); }, (e: Error) => toast(e.message));

  return (
    <div className="app">
      <ReaderHeader paper={data} view={view} canPdf={canPdf} canFlow={canFlow}
        costs={costState.state === "ready" ? costState.data : null}
        bilingual={showBilingual} canTranslate={canTranslate} onBilingual={setBilingual}
        onView={setPreferred} onPure={pure} onHelp={() => setHelp(true)} />
      <div className={`layout${blocks.length && !showBilingual ? "" : " wide"}`}>
        <Toc blocks={blocks} active={activeHeading} onJump={(b) => jumpTo(b, b.page)} />
        <main className={`doc${view === "pdf" && pdf ? " pdf-mode" : ""}`} ref={setScroller} onScroll={onScroll} onMouseUp={onMouseUp}>
          {!data.has_pdf && !data.has_document && (
            <article className="flow"><h1>{data.title}</h1><div className="degraded">还没有全文，下面是摘要。</div><p>{data.abstract ?? "（无摘要）"}</p></article>
          )}
          {view === "pdf" && pdfError && <div className="view-msg">{pdfError}</div>}
          {view === "pdf" && canPdf && !pdf && <div className="view-msg">正在打开 PDF…</div>}
          {view === "pdf" && pdf && <PdfView pdf={pdf} blocks={blocks} annotations={data.annotations} focus={focus} onAction={onBlockAction}
            translation={translation} onPage={savePosition} onSelect={onSelected} />}
          {view === "flow" && doc && <FlowView paper={data} document={doc} pdf={pdf} annotations={data.annotations} focus={focus} scroller={scroller}
            onAction={onBlockAction} onToPdf={(b) => { setPreferred("pdf"); window.setTimeout(() => jumpTo(b, b.page), 60); }}
            translation={translation} />}
        </main>
        <Panel tab={tab} onTab={setTab} paper={data} blocks={blocks} onChange={reload} onMarkRead={markRead}
          onJumpBlock={(b) => jumpTo(b, b.page)}
          onJumpNote={(note) => note.page !== null && jumpTo(locate(blocks, note), note.page, note.quote)} />
      </div>
      {selection && <SelectionBar selection={selection} autoHl={autoHl} onAction={(a) => onSelectionAction(a, selection)} />}
      {popup && <SelectionPopup selection={popup} translations={translations} onClose={() => setPopup(null)}
        onOpenBlock={(index) => { setPopup(null); if (!open.has(index)) toggleOpen(index); }}
        onAsk={() => { setPopup(null); setTab("qa"); toast(SOON.ask); }}
        onNote={() => { const p = popup; setPopup(null); setNoteTarget({ quote: p.text, page: p.page, block: p.block, rects: p.rects }); }} />}
      {noteTarget && <NoteDialog target={noteTarget} onCancel={() => setNoteTarget(null)}
        onSave={(body) => { const t = noteTarget; setNoteTarget(null); window.getSelection()?.removeAllRanges(); void create("note", t, body); }} />}
      {help && <Modal onClose={() => setHelp(false)}><h3 style={{ marginTop: 0 }}>快捷键</h3><pre>{HELP}</pre></Modal>}
    </div>
  );
}
