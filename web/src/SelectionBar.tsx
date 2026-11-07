// 选中文字后的深色工具条（翻译 / 高亮 / 记笔记 / 问 / 连续高亮），和记笔记的对话框。
import { useState } from "react";
import type { TextSelection } from "./selection";
import type { Block, Rect } from "./types";
import { AuthorQuote, Modal } from "./ui";

export interface NoteTarget {
  quote: string | null;
  page: number | null;
  block: Block | null;
  rects: Rect[];
}

const BAR_WIDTH = 320;

export function SelectionBar({ selection, autoHl, onAction }: {
  selection: TextSelection; autoHl: boolean; onAction: (action: "translate" | "highlight" | "note" | "ask" | "auto") => void;
}) {
  const left = Math.max(8, Math.min(selection.anchor.x - BAR_WIDTH / 2, window.innerWidth - BAR_WIDTH - 8));
  const top = Math.max(8, selection.anchor.top - 44);
  return (
    <div className="selbar" style={{ left, top }} role="toolbar" aria-label="选中文字" onMouseDown={(e) => e.preventDefault()}>
      <button onClick={() => onAction("translate")}>翻译</button>
      <button onClick={() => onAction("highlight")}>高亮</button>
      <button onClick={() => onAction("note")}>记笔记</button>
      <button onClick={() => onAction("ask")}>问</button>
      <button className={autoHl ? "on" : ""} title="之后每次选中都直接高亮，不再弹出菜单" onClick={() => onAction("auto")}>连续高亮</button>
    </div>
  );
}

export function NoteDialog({ target, onSave, onCancel }: { target: NoteTarget; onSave: (body: string) => void; onCancel: () => void }) {
  const [body, setBody] = useState("");
  return (
    <Modal onClose={onCancel} width={520}>
      <h3 style={{ marginTop: 0 }}>{target.quote ? "记笔记" : `给第 ${(target.page ?? 0) + 1} 页这段记一条笔记`}</h3>
      {target.quote && <AuthorQuote quote={target.quote} page={null} label="原文" />}
      <label className="lbl" htmlFor="noteBody"><span className="badge b-user">你</span>笔记</label>
      <textarea id="noteBody" className="usernote" autoFocus value={body} onChange={(e) => setBody(e.target.value)}
        onKeyDown={(e) => { if (e.key === "Enter" && (e.metaKey || e.ctrlKey) && body.trim()) onSave(body.trim()); }} />
      <div className="row">
        <button className="primary sm" disabled={!body.trim()} onClick={() => onSave(body.trim())}>保存</button>
        <span className="tag">⌘ + Enter 保存</span>
      </div>
    </Modal>
  );
}
