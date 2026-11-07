// 笔记：高亮的原文和你写的笔记，可检索、能跳回原文。
import { useState } from "react";
import { api } from "../api";
import type { Annotation } from "../types";
import { AuthorQuote, UserText, useToast } from "../ui";

function Note({ item, onChange, onJump }: { item: Annotation; onChange: () => void; onJump: () => void }) {
  const toast = useToast();
  const [editing, setEditing] = useState(false);
  const [draft, setDraft] = useState(item.body ?? "");
  const run = async (action: () => Promise<void>, done: string) => {
    try {
      await action();
      toast(done);
      onChange();
    } catch (error) {
      toast(error instanceof Error ? error.message : "操作失败");
    }
  };
  return (
    <li>
      {item.quote ? (
        <AuthorQuote quote={item.quote} page={item.page} onJump={onJump} label={null} />
      ) : (
        <div className="tag">{item.page !== null ? <>第 {item.page + 1} 页 <span className="link" onClick={onJump}>跳转 ↗</span></> : "整篇笔记"}</div>
      )}
      {editing ? (
        <>
          <label className="tag" htmlFor={`edit-${item.id}`}>笔记</label>
          <textarea id={`edit-${item.id}`} className="usernote" value={draft} onChange={(e) => setDraft(e.target.value)} />
          <div className="edit-row">
            <button className="primary sm" onClick={() => void run(async () => { await api.editAnnotation(item.id, draft.trim() || null); setEditing(false); }, "已保存")}>保存</button>
            <button className="ghost" onClick={() => setEditing(false)}>取消</button>
          </div>
        </>
      ) : (
        item.body && <UserText text={item.body} />
      )}
      {!editing && (
        <div className="annot-actions">
          <button className="ghost" onClick={() => setEditing(true)}>{item.body ? "编辑" : "写笔记"}</button>
          <button className="ghost" onClick={() => void run(() => api.removeAnnotation(item.id), "已删除")}>删除</button>
        </div>
      )}
    </li>
  );
}

export function NotesTab({ paperId, annotations, onChange, onJump }: {
  paperId: string; annotations: Annotation[]; onChange: () => void; onJump: (a: Annotation) => void;
}) {
  const toast = useToast();
  const [filter, setFilter] = useState("");
  const [draft, setDraft] = useState("");
  const q = filter.toLowerCase();
  const list = annotations.filter((a) => !q || `${a.quote ?? ""}${a.body ?? ""}`.toLowerCase().includes(q));
  const add = async () => {
    try {
      await api.addAnnotation(paperId, { kind: "note", body: draft.trim() });
      setDraft("");
      toast("已记到笔记");
      onChange();
    } catch (error) {
      toast(error instanceof Error ? error.message : "保存失败");
    }
  };
  return (
    <>
      <label className="sr-only" htmlFor="noteSearch">搜索笔记</label>
      <input id="noteSearch" className="search" placeholder="搜索笔记和高亮" value={filter} onChange={(e) => setFilter(e.target.value)} />
      {list.length ? (
        <ul className="list">{[...list].reverse().map((a) => <Note key={a.id} item={a} onChange={onChange} onJump={() => onJump(a)} />)}</ul>
      ) : (
        <div className="tag">{filter ? "没有匹配的笔记。" : "选中文字可以高亮或记笔记；悬停段落点“记”也可以。"}</div>
      )}
      <div className="sec" style={{ marginTop: 16 }}>
        <div className="lbl"><span className="badge b-user">你</span>整篇笔记</div>
        <textarea className="usernote" aria-label="整篇笔记" value={draft} onChange={(e) => setDraft(e.target.value)} placeholder="对整篇论文的想法" />
        <div className="row"><button className="primary sm" disabled={!draft.trim()} onClick={() => void add()}>添加</button></div>
      </div>
    </>
  );
}
