// 知识库：概念和掌握程度、你写过的回答、复习（第 12.7、12.8 节）。
// 掌握程度由系统按你的回答和复习算出，你可以直接改；改动本身也会记下来。
import { useEffect, useMemo, useState } from "react";
import { api, topicApi } from "../api";
import { answerNote, dueLabel, translationOf, type ConceptCard } from "../guidance";
import { paperPageLink } from "../pageLink";
import { paths } from "../router";
import type { Mastery } from "../types";
import { UserText, useToast } from "../ui";
import { useAsync } from "../useAsync";
import { MergeSuggestions, Related } from "./ConceptLinks";
import { Review } from "./Review";

export const LEVELS: Record<Mastery, string> = { 0: "没见过", 1: "见过", 2: "能解释", 3: "能应用" };
const ORDER: Mastery[] = [0, 1, 2, 3];
const STACKED = "(max-width: 960px)"; // 与 shell.css 中 .kb 变为单列的断点一致
type Tab = "concepts" | "notes" | "review";

export function MasteryMeter({ level }: { level: Mastery }) {
  return (
    <span className="mastery" title={`掌握程度：${LEVELS[level]}`}>
      <span className="bars3" aria-hidden>{[1, 2, 3].map((n) => <i key={n} className={level >= n ? "on" : ""} />)}</span>
      {LEVELS[level]}
    </span>
  );
}

function Detail({ card, onLevel, onSelect, onReload }: {
  card: ConceptCard; onLevel: (level: Mastery) => void; onSelect: (id: string) => void; onReload: () => void;
}) {
  const { summary, mastery } = card;
  const translation = translationOf(summary);
  return (
    <aside className="panel-card detail" aria-label="概念详情">
      <h3>{summary.concept.name}</h3>
      <div className="z">{translation && `${translation} · `}出现在 {summary.papers} 篇论文里</div>
      <div className="lbl" style={{ marginTop: 0 }}>掌握程度（系统评价，可以改）</div>
      <div className="seg" role="group" aria-label="掌握程度">
        {ORDER.map((n) => <button key={n} aria-pressed={mastery.level === n} onClick={() => onLevel(n)}>{LEVELS[n]}</button>)}
      </div>
      <div className="tag" style={{ marginTop: 4 }}>
        {mastery.overridden ? "你改过等级。" : "读过算“见过”，回答经评价正确才算“能解释”以上。"}
        {mastery.due_at && ` ${dueLabel(mastery.due_at)}。`}
      </div>
      <div className="lbl">论文里的定义</div>
      {summary.definition
        ? (
          <blockquote className="author">
            <span className="badge b-author">作者原话</span> “{summary.definition.quote}”
            <div className="tag" style={{ fontStyle: "normal", marginTop: 4 }}>
              <a className="link" href={paperPageLink(summary.definition.paper_id, summary.definition.page)}>第 {summary.definition.page + 1} 页 ↗</a>
            </div>
          </blockquote>
        )
        : <div className="tag">读过的论文里没有找到核验过的定义。</div>}
      <div className="lbl"><span className="badge b-user">你</span>你写过的回答</div>
      {card.answers.length > 0
        ? card.answers.map((answer) => (
          <div key={answer.card_id} style={{ marginBottom: 8 }}>
            <UserText text={answer.text} />
            {answerNote(answer.verdict, answer.overall) && <div className={`tag ${answer.verdict === "misunderstood" ? "warn-text" : ""}`}>{answerNote(answer.verdict, answer.overall)}</div>}
            <div className="tag">
              回答“{answer.question}” · <a className="link" href={paperPageLink(answer.paper_id, null)}>《{answer.paper_title}》</a>
            </div>
          </div>
        ))
        : <div className="tag">还没有写过。读到相关论文时回答引导问题，就会记在这里。</div>}
      <Related card={card} onSelect={onSelect} onChanged={onReload} />
    </aside>
  );
}

function Concepts({ cards, onChanged, onReload }: { cards: ConceptCard[]; onChanged: (card: ConceptCard) => void; onReload: () => void }) {
  const toast = useToast();
  const [filter, setFilter] = useState<Mastery | "all">("all");
  const [query, setQuery] = useState("");
  const [selected, setSelected] = useState<string | null>(cards[0]?.summary.concept.id ?? null);
  const q = query.trim().toLowerCase();
  const shown = cards.filter((c) =>
    (filter === "all" || c.mastery.level === filter) &&
    (!q || `${c.summary.concept.name} ${translationOf(c.summary)}`.toLowerCase().includes(q)));
  const current = cards.find((c) => c.summary.concept.id === selected) ?? shown[0] ?? null;
  const setLevel = async (card: ConceptCard, level: Mastery) => {
    try {
      const mastery = await api.setMastery(card.summary.concept.id, level);
      onChanged({ ...card, mastery, due: card.due && mastery.due_at !== null });
    } catch (cause) {
      toast(cause instanceof Error ? cause.message : "没能保存，请重试");
    }
  };
  return (
    <>
      <div className="filters" role="group" aria-label="按掌握程度筛选">
        <button className="filter" aria-pressed={filter === "all"} onClick={() => setFilter("all")}>全部<span className="n">{cards.length}</span></button>
        {ORDER.map((n) => (
          <button key={n} className={`filter${cards.some((c) => c.mastery.level === n) ? "" : " empty-filter"}`} aria-pressed={filter === n} onClick={() => setFilter(n)}>
            {LEVELS[n]}<span className="n">{cards.filter((c) => c.mastery.level === n).length}</span>
          </button>
        ))}
        <input className="field" type="search" placeholder="查概念" aria-label="查概念" value={query} onChange={(e) => setQuery(e.target.value)} />
      </div>
      <div className="kb">
        <div role="listbox" aria-label="概念">
          {shown.length === 0 && <div className="empty"><p>没有符合条件的概念。</p></div>}
          {shown.map((c) => (
            <button key={c.summary.concept.id} className="concept" role="option" aria-selected={c.summary.concept.id === current?.summary.concept.id}
              onClick={() => {
                setSelected(c.summary.concept.id);
                // 窄屏时详情排在列表下面：滚过去，否则点了像没反应
                if (window.matchMedia(STACKED).matches) {
                  requestAnimationFrame(() => document.querySelector(".detail")?.scrollIntoView({ behavior: "smooth", block: "start" }));
                }
              }}>
              <span>
                <span className="n">{c.summary.concept.name}</span>
                {translationOf(c.summary) && <span className="z">{translationOf(c.summary)}</span>}
                {c.due && <span className="due">到期复习</span>}
              </span>
              <MasteryMeter level={c.mastery.level} />
            </button>
          ))}
        </div>
        {current && <Detail card={current} onLevel={(level) => void setLevel(current, level)} onSelect={setSelected} onReload={onReload} />}
      </div>
    </>
  );
}

function Notes() {
  const [loaded] = useAsync((signal?: AbortSignal) => api.notes(signal), []);
  const [query, setQuery] = useState("");
  if (loaded.state === "loading") return <div className="skeleton" aria-busy aria-label="正在加载笔记" />;
  if (loaded.state === "error") return <div className="view-msg">{loaded.message}</div>;
  const q = query.trim().toLowerCase();
  const shown = loaded.data.filter(({ annotation: a, paper_title }) =>
    !q || `${a.quote ?? ""} ${a.body ?? ""} ${paper_title}`.toLowerCase().includes(q));
  if (loaded.data.length === 0) {
    return <div className="empty"><h2>还没有笔记</h2><p>读论文时选中文字，点“记”就能高亮或写笔记；问答也可以存成笔记。它们都会汇总到这里。</p></div>;
  }
  return (
    <div>
      <div className="filters">
        <input className="field" type="search" placeholder="查笔记" aria-label="查笔记" value={query} onChange={(e) => setQuery(e.target.value)} />
        <span className="tag">{shown.length} 条</span>
      </div>
      {shown.length === 0 && <div className="empty"><p>没有符合条件的笔记。</p></div>}
      {shown.map(({ annotation: a, paper_title }) => (
        <div className="note-item" key={a.id}>
          <div className="from">
            <a className="link" href={paperPageLink(a.paper_id, a.page)}>《{paper_title}》{a.page !== null ? ` · 第 ${a.page + 1} 页` : ""} ↗</a>
          </div>
          {a.quote && <blockquote className="author">“{a.quote}”</blockquote>}
          {a.body && <UserText text={a.body} />}
        </div>
      ))}
    </div>
  );
}

export function Knowledge() {
  const [tab, setTab] = useState<Tab>(window.location.hash === "#review" ? "review" : "concepts");
  const [topic, setTopic] = useState<string | null>(null);
  const [topics] = useAsync(() => topicApi.list(), []);
  const [loaded, reload] = useAsync(() => api.conceptCards("", topic), [topic]);
  const [cards, setCards] = useState<ConceptCard[]>([]);
  useEffect(() => { if (loaded.state === "ready") setCards(loaded.data.items); }, [loaded]);
  const due = useMemo(() => cards.filter((c) => c.due).length, [cards]);
  const head = (
    <div className="screen-head">
      <h1>知识库</h1><span className="sub">你弄懂的概念、写过的回答和笔记</span>
      <a className="ghost head-action" href={`${paths.settings}#export`}>导出 Markdown</a>
    </div>
  );
  if (loaded.state === "loading") return <div className="screen-in">{head}<div className="skeleton" aria-busy /></div>;
  if (loaded.state === "error") return <div className="screen-in">{head}<div className="view-msg">{loaded.message}</div></div>;
  const tabs: [Tab, string][] = [["concepts", "概念"], ["notes", "笔记"], ["review", due ? `复习 ${due}` : "复习"]];
  const replace = (next: ConceptCard) => setCards((items) => items.map((c) => (c.summary.concept.id === next.summary.concept.id ? next : c)));
  return (
    <div className="screen-in wide">
      {head}
      {topics.state === "ready" && topics.data.length > 0 && tab === "concepts" && (
        <div className="filters" role="group" aria-label="按课题看">
          <button className="filter" aria-pressed={topic === null} onClick={() => setTopic(null)}>全部课题</button>
          {topics.data.map((t) => (
            <button key={t.id} className="filter" aria-pressed={topic === t.id} onClick={() => setTopic(t.id)}>{t.name}</button>
          ))}
        </div>
      )}
      <div className="subtabs" role="tablist" style={{ marginTop: 0 }}>
        {tabs.map(([key, text]) => <button key={key} role="tab" aria-selected={tab === key} onClick={() => setTab(key)}>{text}</button>)}
      </div>
      {tab === "concepts" && <MergeSuggestions version={cards.length} onChanged={reload} />}
      {tab === "concepts" && (cards.length > 0 ? <Concepts cards={cards} onChanged={replace} onReload={reload} /> : (
        topic ? (
          <div className="empty">
            <h2>这个课题下还没有概念</h2>
            <p>课题里的论文提炼出要点后，关键概念会出现在这里。<button className="link-btn" onClick={() => setTopic(null)}>看全部课题</button></p>
          </div>
        ) : <div className="empty">
          <h2>还没有概念</h2>
          <p>打开一篇论文，系统会找出其中的关键概念。读完用自己的话回答导读里的引导问题，你弄懂的概念和写下的话就会沉淀在这里，并按遗忘规律提醒你复习。</p>
        </div>
      ))}
      {tab === "notes" && <Notes />}
      {tab === "review" && <Review onDone={reload} />}
    </div>
  );
}
