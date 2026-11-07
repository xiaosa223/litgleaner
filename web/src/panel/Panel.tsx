// 侧栏：导读、读后卡片、问答、笔记四个标签页。
import type { Annotation, Block, PaperView } from "../types";
import { SOON } from "../ui";
import { CardTab } from "./CardTab";
import { GuideTab } from "./GuideTab";
import { NotesTab } from "./NotesTab";
import { QaTab } from "./QaTab";

export type Tab = "guide" | "card" | "qa" | "notes";
const TABS: [Tab, string][] = [["guide", "导读"], ["card", "读后卡片"], ["qa", "问答"], ["notes", "笔记"]];

export function Panel(props: {
  tab: Tab; onTab: (tab: Tab) => void; paper: PaperView; blocks: Block[];
  onChange: () => void; onJumpBlock: (block: Block) => void; onJumpNote: (note: Annotation) => void; onMarkRead: () => void;
}) {
  const { tab, onTab, paper, blocks, onChange, onJumpBlock, onJumpNote, onMarkRead } = props;
  return (
    <aside className="panel">
      <div className="tabs" role="tablist">
        {TABS.map(([key, label]) => (
          <button key={key} role="tab" aria-selected={tab === key} onClick={() => onTab(key)}>
            {label}{key === "notes" && paper.annotations.length > 0 ? ` ${paper.annotations.length}` : ""}
          </button>
        ))}
      </div>
      <div className="tabbody">
        {tab === "guide" && <GuideTab blocks={blocks} onJump={onJumpBlock} />}
        {tab === "card" && <CardTab paper={paper} onMarkRead={onMarkRead} />}
        {tab === "qa" && <QaTab />}
        {tab === "notes" && <NotesTab paperId={paper.paper_id} annotations={paper.annotations} onChange={onChange} onJump={onJumpNote} />}
      </div>
      {tab === "qa" && (
        <div className="ask">
          <textarea id="askInput" disabled placeholder={SOON.ask} aria-label="提问" />
          <button className="primary" disabled>问</button>
        </div>
      )}
    </aside>
  );
}
