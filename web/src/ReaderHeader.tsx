// 阅读页顶栏：只放返回、目录、标题、原文与译文的切换、“显示”面板、侧栏和帮助（第 12.8 节）。
import { ArrowLeft, Keyboard, PanelLeft, PanelRight, SlidersHorizontal } from "lucide-react";
import { useEffect, useRef, useState } from "react";
import type { TranslateMode } from "./prefs";
import { goBack, paths } from "./router";
import type { PaperView } from "./types";

export type View = "pdf" | "flow";
export interface Costs { month_usd: number; paper_usd: number | null }

function DisplayMenu({ view, canPdf, canFlow, costs, onView, onPure, onTranslateAll }: {
  view: View; canPdf: boolean; canFlow: boolean; costs: Costs | null; onView: (view: View) => void; onPure: () => void;
  onTranslateAll: (() => void) | null;
}) {
  const [open, setOpen] = useState(false);
  const root = useRef<HTMLDivElement>(null);
  useEffect(() => {
    if (!open) return;
    const onDown = (event: MouseEvent) => !root.current?.contains(event.target as Node) && setOpen(false);
    const onKey = (event: KeyboardEvent) => event.key === "Escape" && setOpen(false);
    window.addEventListener("mousedown", onDown);
    window.addEventListener("keydown", onKey);
    return () => {
      window.removeEventListener("mousedown", onDown);
      window.removeEventListener("keydown", onKey);
    };
  }, [open]);
  return (
    <div className="menu-root" ref={root}>
      <button className="ghost iconbtn" aria-haspopup="true" aria-expanded={open} onClick={() => setOpen(!open)} title="显示设置">
        <SlidersHorizontal size={15} aria-hidden /><span className="btn-lbl">显示</span>
      </button>
      {open && (
        <div className="menu" role="group" aria-label="显示设置">
          <div className="menu-row">
            <span>视图</span>
            <div className="seg">
              <button aria-pressed={view === "pdf"} disabled={!canPdf} title="保留 PDF 版式（1）" onClick={() => onView("pdf")}>原版</button>
              <button aria-pressed={view === "flow"} disabled={!canFlow} title="重排文字，适合小屏（2）" onClick={() => onView("flow")}>段落</button>
            </div>
          </div>
          {onTranslateAll && (
            <button className="menu-item" onClick={() => { onTranslateAll(); setOpen(false); }}>翻译全文…</button>
          )}
          <button className="menu-item" onClick={() => { onPure(); setOpen(false); }}>
            回到纯原文<kbd>Esc</kbd>
          </button>
          {costs && (
            <div className="menu-foot" title="模型花费（美元）">
              模型花费：{costs.paper_usd !== null && `本篇 $${costs.paper_usd.toFixed(3)} · `}本月 ${costs.month_usd.toFixed(2)}
            </div>
          )}
        </div>
      )}
    </div>
  );
}

export function ReaderHeader(props: {
  paper: PaperView; view: View; canPdf: boolean; canFlow: boolean; costs: Costs | null;
  mode: TranslateMode; canTranslate: boolean; onMode: (mode: TranslateMode) => void;
  toc: { available: boolean; open: boolean; toggle: () => void }; panel: { open: boolean; toggle: () => void };
  onView: (view: View) => void; onPure: () => void; onHelp: () => void;
  onTranslateAll: () => void; progress: string | null; // 后台整篇翻译的进度
}) {
  const { paper, view, canPdf, canFlow, costs, mode, canTranslate, onMode, toc, panel, onView, onPure, onHelp, onTranslateAll, progress } = props;
  const small = [paper.venue, paper.published_at?.slice(0, 4), paper.has_pdf ? null : "只有摘要"].filter(Boolean).join(" · ");
  return (
    <header className="bar">
      <button className="ghost iconbtn" onClick={() => goBack(paths.today)} title="返回" aria-label="返回"><ArrowLeft size={16} aria-hidden /></button>
      {toc.available && (
        <button className="ghost iconbtn" aria-pressed={toc.open} onClick={toc.toggle} title="目录（[）" aria-label="目录"><PanelLeft size={16} aria-hidden /></button>
      )}
      <div className="title" title={paper.title}>{paper.title}{small && <small>{small}</small>}</div>
      {canTranslate && (
        <div className="seg" role="group" aria-label="翻译">
          <button aria-pressed={mode === "off"} onClick={() => onMode("off")}>原文</button>
          {view === "pdf" && (
            <button aria-pressed={mode === "overlay"} title="译文盖在原文位置上，保留版式；鼠标移到段落上看原文" onClick={() => onMode("overlay")}>覆盖</button>
          )}
          <button aria-pressed={mode === "column"} title="原文和译文逐段对照（T）；选中句子也可以单独翻译" onClick={() => onMode("column")}>对照</button>
        </div>
      )}
      {progress && <span className="tag" role="status" title="后台整篇翻译">{progress}</span>}
      <DisplayMenu view={view} canPdf={canPdf} canFlow={canFlow} costs={costs} onView={onView} onPure={onPure}
        onTranslateAll={canTranslate ? onTranslateAll : null} />
      <button className="ghost iconbtn" aria-pressed={panel.open} onClick={panel.toggle} title="侧栏（]）" aria-label="侧栏"><PanelRight size={16} aria-hidden /></button>
      <button className="ghost iconbtn opt" onClick={onHelp} title="快捷键（?）" aria-label="快捷键"><Keyboard size={16} aria-hidden /></button>
    </header>
  );
}
