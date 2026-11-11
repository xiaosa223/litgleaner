// 知识库：概念和掌握程度、全部笔记、复习（第 12.7、12.8 节）。
// 后端还没有知识库接口：开启界面预览时显示示例数据，否则显示空状态。
import { useState } from "react";
import { SAMPLE_CONCEPTS, SAMPLE_NOTES } from "../preview/sample";
import { usePreview } from "../preview/usePreview";
import type { Concept, Mastery } from "../types";
import { PreviewNote, SourcedQuote, UserText } from "../ui";
import { Review } from "./Review";

export const LEVELS: Record<Mastery, string> = { 0: "没见过", 1: "见过", 2: "能解释", 3: "能应用" };
const ORDER: Mastery[] = [0, 1, 2, 3];
type Tab = "concepts" | "notes" | "review";

export function MasteryMeter({ level }: { level: Mastery }) {
  return (
    <span className="mastery" title={`掌握程度：${LEVELS[level]}`}>
      <span className="bars3" aria-hidden>{[1, 2, 3].map((n) => <i key={n} className={level >= n ? "on" : ""} />)}</span>
      {LEVELS[level]}
    </span>
  );
}

function Detail({ concept, level, onLevel }: { concept: Concept; level: Mastery; onLevel: (level: Mastery) => void }) {
  return (
    <aside className="panel-card detail" aria-label="概念详情">
      <h3>{concept.name}</h3>
      <div className="z">{concept.translation} · 出现在 {concept.papers} 篇论文里</div>
      <div className="lbl" style={{ marginTop: 0 }}>掌握程度（系统评价，可以改）</div>
      <div className="seg" role="group" aria-label="掌握程度">
        {ORDER.map((n) => <button key={n} aria-pressed={level === n} onClick={() => onLevel(n)}>{LEVELS[n]}</button>)}
      </div>
      <div className="lbl">论文里的定义</div>
      {concept.definition
        ? <SourcedQuote {...concept.definition} />
        : <div className="tag">读过的论文里没有给出定义。</div>}
      <div className="lbl"><span className="badge b-user">你</span>你写过的回答</div>
      {concept.answer
        ? <UserText text={concept.answer} />
        : <div className="tag">还没有写过。读到相关论文时回答引导问题，就会记在这里。</div>}
      {concept.related.length > 0 && (
        <>
          <div className="lbl">相关概念</div>
          <div className="rel">{concept.related.map((r) => <span key={r.name}><b>{r.kind}</b>{r.name}</span>)}</div>
        </>
      )}
    </aside>
  );
}

function Concepts() {
  const [levels, setLevels] = useState<Record<string, Mastery>>({});
  const [filter, setFilter] = useState<Mastery | "all">("all");
  const [query, setQuery] = useState("");
  const [selected, setSelected] = useState(SAMPLE_CONCEPTS[0].id);
  const levelOf = (c: Concept) => levels[c.id] ?? c.level;
  const q = query.trim().toLowerCase();
  const shown = SAMPLE_CONCEPTS.filter((c) =>
    (filter === "all" || levelOf(c) === filter) && (!q || `${c.name} ${c.translation}`.toLowerCase().includes(q)));
  const current = SAMPLE_CONCEPTS.find((c) => c.id === selected) ?? null;
  return (
    <>
      <div className="filters" role="group" aria-label="按掌握程度筛选">
        <button className="filter" aria-pressed={filter === "all"} onClick={() => setFilter("all")}>全部<span className="n">{SAMPLE_CONCEPTS.length}</span></button>
        {ORDER.map((n) => (
          <button key={n} className="filter" aria-pressed={filter === n} onClick={() => setFilter(n)}>
            {LEVELS[n]}<span className="n">{SAMPLE_CONCEPTS.filter((c) => levelOf(c) === n).length}</span>
          </button>
        ))}
        <input className="field" type="search" placeholder="查概念" aria-label="查概念" value={query} onChange={(e) => setQuery(e.target.value)} />
      </div>
      <div className="kb">
        <div role="listbox" aria-label="概念">
          {shown.length === 0 && <div className="empty"><p>没有符合条件的概念。</p></div>}
          {shown.map((c) => (
            <button key={c.id} className="concept" role="option" aria-selected={c.id === selected} onClick={() => setSelected(c.id)}>
              <span><span className="n">{c.name}</span><span className="z">{c.translation}</span>{c.due && <span className="due">到期复习</span>}</span>
              <MasteryMeter level={levelOf(c)} />
            </button>
          ))}
        </div>
        {current && <Detail concept={current} level={levelOf(current)} onLevel={(level) => setLevels({ ...levels, [current.id]: level })} />}
      </div>
    </>
  );
}

function Notes() {
  return (
    <div>
      {SAMPLE_NOTES.map((note) => (
        <div className="note-item" key={note.id}>
          <div className="from">{note.paper} · 第 {note.page} 页</div>
          <blockquote className="author">“{note.quote}”</blockquote>
          {note.body && <UserText text={note.body} />}
        </div>
      ))}
    </div>
  );
}

export function Knowledge() {
  const [preview] = usePreview();
  const [tab, setTab] = useState<Tab>(window.location.hash === "#review" ? "review" : "concepts");
  const head = <div className="screen-head"><h1>知识库</h1><span className="sub">你弄懂的概念、写过的回答和笔记</span></div>;
  if (!preview) {
    return (
      <div className="screen-in">
        {head}
        <div className="empty">
          <h2>知识库还是空的</h2>
          <p>读完一篇论文，用自己的话回答引导问题，弄懂的概念和你写下的话就会沉淀在这里。</p>
        </div>
      </div>
    );
  }
  const due = SAMPLE_CONCEPTS.filter((c) => c.due);
  const tabs: [Tab, string][] = [["concepts", "概念"], ["notes", "笔记"], ["review", `复习 ${due.length}`]];
  return (
    <div className="screen-in wide">
      {head}
      <PreviewNote />
      <div className="subtabs" role="tablist" style={{ marginTop: 0 }}>
        {tabs.map(([key, text]) => <button key={key} role="tab" aria-selected={tab === key} onClick={() => setTab(key)}>{text}</button>)}
      </div>
      {tab === "concepts" && <Concepts />}
      {tab === "notes" && <Notes />}
      {tab === "review" && <Review concepts={due} />}
    </div>
  );
}
