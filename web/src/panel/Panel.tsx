// 侧栏：按阅读顺序排的标签页：导读、问答、笔记、读后（第 12.2 节）。
import type { Annotation, PaperView } from "../types";
import { CardTab } from "./CardTab";
import { GuideTab } from "./GuideTab";
import { NotesTab } from "./NotesTab";
import { QaTab } from "./QaTab";
import type { QaCitation, QaSelection } from "../qa";

export type Tab = "guide" | "qa" | "notes" | "card";

export function Panel(props: {
  open: boolean; tab: Tab; onTab: (tab: Tab) => void; paper: PaperView; canAsk: boolean;
  askQuote: QaSelection | null; onAskQuote: (quote: QaSelection | null) => void;
  onJumpCitation: (citation: QaCitation) => void;
  onAnswered: () => void;
  onChange: () => void; onJumpNote: (note: Annotation) => void; onMarkRead: () => void;
}) {
  const { open, tab, onTab, paper, canAsk, askQuote, onAskQuote, onChange, onJumpNote, onMarkRead, onJumpCitation, onAnswered } = props;
  const tabs: [Tab, string][] = [
    ["guide", "导读"],
    ...(canAsk ? [["qa", "问答"] as [Tab, string]] : []),
    ["notes", `笔记${paper.annotations.length > 0 ? ` ${paper.annotations.length}` : ""}`],
    ["card", "读后"],
  ];
  const current = tabs.some(([key]) => key === tab) ? tab : "guide";
  return (
    <aside className="panel" hidden={!open} aria-label="侧栏">
      <div className="tabs" role="tablist">
        {tabs.map(([key, label]) => <button key={key} role="tab" aria-selected={current === key} onClick={() => onTab(key)}>{label}</button>)}
      </div>
      {canAsk && <div className="qa-panel" hidden={current !== "qa"}>
        <QaTab paperId={paper.paper_id} selected={askQuote} onSelection={onAskQuote} onJump={onJumpCitation} onChange={onChange} onAnswered={onAnswered}
          savedQuestions={paper.annotations.flatMap((note) => note.qa_question_id ? [note.qa_question_id] : [])} />
      </div>}
      {current !== "qa" && (
          <div className="tabbody">
            {current === "guide" && <GuideTab paper={paper} />}
            {current === "notes" && <NotesTab paperId={paper.paper_id} annotations={paper.annotations} onChange={onChange} onJump={onJumpNote} />}
            {current === "card" && <CardTab paper={paper} onMarkRead={onMarkRead} />}
          </div>
        )}
    </aside>
  );
}
