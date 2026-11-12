// 阅读页：顶栏 + 目录 | 正文（原版或段落视图）| 侧栏。正文优先：目录和侧栏可以折叠（第 12.8 节）。
import { useCallback, useEffect, useRef, useState } from "react";
import { api } from "./api";
import { FlowView, type FlowTranslation } from "./FlowView";
import { Panel, type Tab } from "./panel/Panel";
import { pageFromSearch } from "./pageLink";
import { usePdf } from "./pdf";
import { MOD } from "./platform";
import { usePreview } from "./preview/usePreview";
import { PdfView, type BlockAction, type Focus } from "./PdfView";
import { useMedia, usePref, useTranslateMode, type TranslateMode } from "./prefs";
import { ReaderHeader, type View } from "./ReaderHeader";
import { SelectionBar, NoteDialog, type NoteTarget } from "./SelectionBar";
import { readSelection, type TextSelection } from "./selection";
import { SelectionPopup } from "./SelectionPopup";
import { useTranslations } from "./translations";
import { useWholeTranslation, WholeTranslationDialog } from "./WholeTranslation";
import { Toc } from "./Toc";
import type { Annotation, Block } from "./types";
import { Modal, useToast } from "./ui";
import { useColumn } from "./useColumn";
import { useAsync } from "./useAsync";
import { currentPage, useReadingPosition } from "./useReadingPosition";

const KEYS: [string, string][] = [
  ["选中文字", "翻译、高亮、记笔记"],
  ["T", "原文 ↔ 上次用的翻译方式"],
  ["1 / 2", "原版视图 / 段落视图"],
  ["[ / ]", "收起或展开目录 / 侧栏"],
  [`${MOD} K`, "搜索"],
  ["Esc", "关闭浮层；再按一次回到纯原文"],
  ["?", "显示这张表"],
];
const QUOTE_LIMIT = 300; // 带进提问的原文片段长度

function locate(blocks: Block[], note: Annotation): Block | null {
  const same = (b: Block) => b.page === note.page && note.bbox !== null && b.bbox !== null && b.bbox.every((v, i) => v === note.bbox![i]);
  const quoted = (b: Block) => b.page === note.page && !!note.quote && b.text.replace(/\s+/g, " ").includes(note.quote);
  return blocks.find(same) ?? blocks.find(quoted) ?? null;
}

export function Reader({ paperId }: { paperId: string }) {
  const toast = useToast();
  const [paperState, reload] = useAsync(() => api.paper(paperId), [paperId]);
  const paper = paperState.state === "ready" ? paperState.data : null;
  // 明确登记首次打开，不依赖翻页或滚动；失败保留提示，再次打开时可重试。
  useEffect(() => {
    let active = true;
    void api.openPaper(paperId).then(
      () => { if (active) reload(); },
      (error: unknown) => { if (active) toast(error instanceof Error ? error.message : "阅读状态保存失败，请重新打开重试"); },
    );
    return () => { active = false; };
  }, [paperId, reload, toast]);
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
  const [wholeOpen, setWholeOpen] = useState(false);
  const [activeHeading, setActiveHeading] = useState<number | null>(null);
  const [restored, setRestored] = useState(false);
  const [requestedPage, setRequestedPage] = useState(() => ({ page: pageFromSearch(window.location.search) }));
  const appliedPage = useRef<typeof requestedPage | null>(null);
  const [mode, setMode] = useTranslateMode();
  const lastMode = useRef<TranslateMode>(mode === "off" ? "column" : mode); // T 键在原文和上次的方式之间切换
  const [canAsk] = usePreview(); // 边读边问的服务还没有接上，只在界面预览里出现
  const [askQuote, setAskQuote] = useState<string | null>(null);
  const [open, setOpen] = useState(new Set<number>());
  const [hoverSentence, setHoverSentence] = useState<string | null>(null);
  const [popup, setPopup] = useState<TextSelection | null>(null);

  const blocks = doc?.blocks ?? [];
  const translations = useTranslations(paperId, !!doc, reloadCosts);
  const canPdf = !!paper?.has_pdf && !pdfError, canFlow = !!doc;
  const view: View = preferred === "flow" && canFlow ? "flow" : canPdf ? "pdf" : canFlow ? "flow" : "pdf";
  const canTranslate = blocks.some((b) => b.translatable);
  const whole = useWholeTranslation(paperId, canTranslate, translations.reload);
  // 段落视图没有版面可覆盖，全文覆盖在那里按双栏对照显示
  const showOverlay = mode === "overlay" && canTranslate && view === "pdf";
  const showBilingual = canTranslate && mode !== "off" && !showOverlay;
  const chooseMode = (next: TranslateMode) => {
    if (next !== "off") lastMode.current = next;
    setMode(next);
  };
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
    translations, bilingual: showBilingual, overlay: showOverlay, open, onToggle: toggleOpen,
    pairing: { hover: hoverSentence, onHover: setHoverSentence },
  };
  const savePosition = useReadingPosition(paper, view, reload);
  const hasToc = blocks.some((b) => b.kind === "heading");
  // 目录在 1180 像素以上停靠在左侧，侧栏在 820 像素以上停靠在右侧；更窄时是浮层，默认收起
  const tocDocked = useMedia("(min-width: 1180px)");
  const toc = useColumn("toc", { docked: tocDocked, autoOpen: useMedia("(min-width: 1440px)"), yielding: showBilingual });
  const panel = useColumn("panel", { docked: useMedia("(min-width: 820px)"), autoOpen: useMedia("(min-width: 1024px)"), yielding: showBilingual });
  const ask = (quote: string | null) => {
    setAskQuote(quote ? quote.slice(0, QUOTE_LIMIT) : null);
    setTab("qa");
    panel.show();
  };

  const jumpTo = useCallback((block: Block | null, page: number, quote: string | null = null) => {
    if (view === "flow" && !block) block = blocks.find((b) => b.index >= (doc?.body_start ?? 0) && b.kind !== "reference" && b.page >= page) ?? null;
    setFocus({ block: block?.index ?? null, page, quote, nonce: Date.now() });
  }, [view, blocks, doc?.body_start]);

  useEffect(() => {
    const update = () => setRequestedPage({ page: pageFromSearch(window.location.search) });
    window.addEventListener("popstate", update);
    return () => window.removeEventListener("popstate", update);
  }, []);

  // 检索结果可以跳回原文，同一篇论文内再次检索也能跳；不写阅读状态。
  useEffect(() => {
    if (requestedPage.page === null || appliedPage.current === requestedPage || !paper || !scroller || (view === "pdf" ? !pdf : !doc)) return;
    appliedPage.current = requestedPage;
    setRestored(true);
    const lastPage = pdf ? pdf.sizes.length - 1 : Math.max(0, ...blocks.map((block) => block.page));
    jumpTo(null, Math.min(requestedPage.page, Math.max(0, lastPage)));
  }, [requestedPage, paper, scroller, view, pdf, doc, blocks, jumpTo]);

  // 打开时回到上次读到的位置
  useEffect(() => {
    if (requestedPage.page !== null || restored || !paper || !scroller || (view === "pdf" ? !pdf : !doc)) return;
    setRestored(true);
    const page = paper.reading?.position?.page;
    if (typeof page === "number" && page > 0) jumpTo(null, page);
  }, [requestedPage, restored, paper, scroller, view, pdf, doc, jumpTo]);

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
    if (action === "ask") { sel.clear(); ask(sel.text); return; }
    if (action === "note") { setNoteTarget(target); return; }
    if (action === "auto") { setAutoHl(true); toast("连续高亮已开启：选中即高亮，按 Esc 关闭"); }
    sel.clear();
    void create("highlight", target, null);
  };

  const onBlockAction = (action: BlockAction, block: Block) => {
    if (action === "ask") { ask(block.text); return; }
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
      // 输入框里打字、按着修饰键（如搜索的快捷键）、或有弹窗和菜单时，不触发阅读页的快捷键；它们自己处理 Esc
      if ((event.target as HTMLElement).matches("textarea, input, select")) return;
      if (event.metaKey || event.ctrlKey || event.altKey || document.querySelector(".modal, .menu")) return;
      if (event.key === "1" && canPdf) setPreferred("pdf");
      if (event.key === "2" && canFlow) setPreferred("flow");
      if (event.key === "/" && canAsk) { event.preventDefault(); ask(null); }
      if ((event.key === "t" || event.key === "T") && canTranslate) chooseMode(mode === "off" ? lastMode.current : "off");
      if (event.key === "[" && hasToc) toc.toggle();
      if (event.key === "]") panel.toggle();
      if (event.key === "?") setHelp(true);
      if (event.key === "Escape") {
        if (selection || popup) { setSelection(null); setPopup(null); return; }
        if (autoHl) { setAutoHl(false); toast("已关闭连续高亮"); return; }
        pure();
      }
    };
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  });

  if (paperState.state === "loading") return <div className="view-msg">正在加载…</div>;
  if (paperState.state === "error") return <div className="view-msg">{paperState.message}</div>;
  const data = paperState.data;
  function pure() {
    chooseMode("off");
    setOpen(new Set());
    toast("已回到纯原文，可以随时重新打开各项增强");
  }
  const markRead = () => void api.setReading(paperId, "read").then(() => { toast("已标记为已读"); reload(); }, (e: Error) => toast(e.message));

  return (
    <div className="app">
      <ReaderHeader paper={data} view={view} canPdf={canPdf} canFlow={canFlow}
        costs={costState.state === "ready" ? costState.data : null}
        mode={mode === "off" ? "off" : showOverlay ? "overlay" : "column"} canTranslate={canTranslate} onMode={chooseMode}
        toc={{ available: hasToc, open: toc.open, toggle: toc.toggle }} panel={panel}
        onView={setPreferred} onPure={pure} onHelp={() => setHelp(true)}
        onTranslateAll={() => { setWholeOpen(true); void whole.refresh(); }}
        progress={whole.running && whole.plan ? `全文翻译中 · 剩 ${whole.plan.pending} 段` : null} />
      <div className={`layout${hasToc && toc.open ? " has-toc" : ""}${panel.open ? " has-panel" : ""}`}>
        {hasToc && toc.open && (
          // 目录是浮层时，跳转后收起，不挡正文
          <Toc blocks={blocks} active={activeHeading} onJump={(b) => { jumpTo(b, b.page); if (!tocDocked) toc.toggle(); }} />
        )}
        <main className={`doc${view === "pdf" && pdf ? " pdf-mode" : ""}`} ref={setScroller} onScroll={onScroll} onMouseUp={onMouseUp}>
          {!data.has_pdf && !data.has_document && (
            <article className="flow"><h1>{data.title}</h1><div className="degraded">还没有全文，下面是摘要。</div><p>{data.abstract ?? "（无摘要）"}</p></article>
          )}
          {view === "pdf" && pdfError && <div className="view-msg">{pdfError}</div>}
          {view === "pdf" && canPdf && !pdf && <div className="view-msg">正在打开 PDF…</div>}
          {view === "pdf" && pdf && <PdfView pdf={pdf} blocks={blocks} annotations={data.annotations} focus={focus} onAction={onBlockAction}
            translation={translation} onPage={savePosition} onSelect={onSelected} canAsk={canAsk} />}
          {view === "flow" && doc && <FlowView paper={data} document={doc} pdf={pdf} annotations={data.annotations} focus={focus} scroller={scroller}
            onAction={onBlockAction} onToPdf={(b) => { setPreferred("pdf"); window.setTimeout(() => jumpTo(b, b.page), 60); }}
            translation={translation} canAsk={canAsk} />}
        </main>
        <Panel open={panel.open} tab={tab} onTab={setTab} paper={data} canAsk={canAsk} askQuote={askQuote} onAskQuote={setAskQuote}
          onChange={reload} onMarkRead={markRead}
          onJumpNote={(note) => note.page !== null && jumpTo(locate(blocks, note), note.page, note.quote)} />
      </div>
      {selection && <SelectionBar selection={selection} autoHl={autoHl} canAsk={canAsk} onAction={(a) => onSelectionAction(a, selection)} />}
      {popup && <SelectionPopup selection={popup} translations={translations} onClose={() => setPopup(null)}
        onOpenBlock={(index) => { setPopup(null); if (!open.has(index)) toggleOpen(index); }}
        onAsk={canAsk ? () => { const p = popup; setPopup(null); ask(p.text); } : null}
        onNote={() => { const p = popup; setPopup(null); setNoteTarget({ quote: p.text, page: p.page, block: p.block, rects: p.rects }); }} />}
      {noteTarget && <NoteDialog target={noteTarget} onCancel={() => setNoteTarget(null)}
        onSave={(body) => { const t = noteTarget; setNoteTarget(null); window.getSelection()?.removeAllRanges(); void create("note", t, body); }} />}
      {wholeOpen && <WholeTranslationDialog plan={whole.plan} onConfirm={whole.confirm} onClose={() => setWholeOpen(false)} />}
      {help && (
        <Modal onClose={() => setHelp(false)} width={440}>
          <h3 className="dialog-title">快捷键</h3>
          <table className="keys"><tbody>
            {[...KEYS, ...(canAsk ? [["/", "提问"] as [string, string]] : [])].map(([key, what]) => <tr key={key}><td><kbd>{key}</kbd></td><td>{what}</td></tr>)}
          </tbody></table>
        </Modal>
      )}
    </div>
  );
}
