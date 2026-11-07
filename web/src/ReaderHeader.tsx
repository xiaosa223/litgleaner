import { navigate } from "./router";
import type { PaperView } from "./types";
import { SOON } from "./ui";

export type View = "pdf" | "flow";
const FACETS: [string, string][] = [["goal", "目标"], ["novelty", "创新"], ["method", "方法"], ["result", "结果"]];

export function ReaderHeader(props: {
  paper: PaperView; view: View; canPdf: boolean; canFlow: boolean; costs: { month_usd: number; paper_usd: number | null } | null;
  bilingual: boolean; canTranslate: boolean; onBilingual: (on: boolean) => void;
  onView: (view: View) => void; onPure: () => void; onHelp: () => void;
}) {
  const { paper, view, canPdf, canFlow, costs, bilingual, canTranslate, onBilingual, onView, onPure, onHelp } = props;
  const small = [paper.venue, paper.published_at, paper.has_pdf ? "有全文" : "只有摘要"].filter(Boolean).join(" · ");
  return (
    <header className="bar">
      <button className="ghost" onClick={() => navigate("/")}>← 收件箱</button>
      <div className="title" title={paper.title}>{paper.title}<small>{small}</small></div>
      <div className="group">
        <label>视图</label>
        <div className="seg">
          <button aria-pressed={view === "pdf"} disabled={!canPdf} onClick={() => onView("pdf")}>原版</button>
          <button aria-pressed={view === "flow"} disabled={!canFlow} onClick={() => onView("flow")}>段落</button>
        </div>
      </div>
      <div className="group">
        <label>翻译</label>
        <div className="seg">
          <button aria-pressed={!bilingual} onClick={() => onBilingual(false)}>原文</button>
          <button aria-pressed={bilingual} disabled={!canTranslate} title={canTranslate ? "整篇逐段对照（T）" : "这篇没有可翻译的正文"}
            onClick={() => onBilingual(true)}>双语对照</button>
        </div>
        <span className="hint">选中句子即可翻译</span>
      </div>
      <div className="group">
        {FACETS.map(([key, label]) => (
          <button key={key} className="chip" aria-pressed="false" disabled title={SOON.facets}>
            <i style={{ background: `var(--${key}-bar)` }} />{label}
          </button>
        ))}
      </div>
      <button className="chip" aria-pressed="false" disabled title={SOON.terms}>术语释义</button>
      <button className="ghost" onClick={onPure} title="关闭所有增强（Esc）">纯原文</button>
      {costs && (
        <span className="cost" title="模型花费（美元）">
          {costs.paper_usd !== null && `本篇 $${costs.paper_usd.toFixed(3)} · `}本月 ${costs.month_usd.toFixed(2)}
        </span>
      )}
      <button className="ghost" onClick={onHelp} title="快捷键">?</button>
    </header>
  );
}
