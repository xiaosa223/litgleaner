// 侧栏：按阅读顺序排的标签页：导读、问答、笔记、读后（第 12.2 节）。
// 问答的服务还没有接上，只在界面预览开启时出现。
import { useState } from "react";
import type { Annotation, PaperView } from "../types";
import { CardTab } from "./CardTab";
import { GuideTab } from "./GuideTab";
import { NotesTab } from "./NotesTab";
import { QaTab, type QaMessage } from "./QaTab";

export type Tab = "guide" | "qa" | "notes" | "card";

export function Panel(props: {
  open: boolean; tab: Tab; onTab: (tab: Tab) => void; paper: PaperView; canAsk: boolean;
  askQuote: string | null; onAskQuote: (quote: string | null) => void;
  onChange: () => void; onJumpNote: (note: Annotation) => void; onMarkRead: () => void;
}) {
  const { open, tab, onTab, paper, canAsk, askQuote, onAskQuote, onChange, onJumpNote, onMarkRead } = props;
  const [messages, setMessages] = useState<QaMessage[]>([]);
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
      {current === "qa"
        ? <QaTab paperId={paper.paper_id} messages={messages} onMessages={setMessages} quote={askQuote} onQuote={onAskQuote} />
        : (
          <div className="tabbody">
            {current === "guide" && <GuideTab paper={paper} />}
            {current === "notes" && <NotesTab paperId={paper.paper_id} annotations={paper.annotations} onChange={onChange} onJump={onJumpNote} />}
            {current === "card" && <CardTab paper={paper} onMarkRead={onMarkRead} />}
          </div>
        )}
    </aside>
  );
}
