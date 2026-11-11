// 文库：读过、在读、待读的论文都在这里，按状态、课题和文字筛选（第 12.8 节）。
import { useMemo, useState } from "react";
import { countByStatus, filterLibrary, mergeLibrary, type StatusFilter } from "../library";
import { PaperRow, STATUS_LABEL } from "../PaperCard";
import type { Inbox } from "../types";
import type { Async } from "../useAsync";

const FILTERS: StatusFilter[] = ["all", "reading", "queued", "new", "read", "abandoned"];
const label = (status: StatusFilter) => (status === "all" ? "全部" : STATUS_LABEL[status]);

export function Library({ inbox, reload, onAdd }: { inbox: Async<Inbox>; reload: () => void; onAdd: () => void }) {
  const [status, setStatus] = useState<StatusFilter>("all");
  const [topicId, setTopicId] = useState<string | null>(null);
  const [query, setQuery] = useState("");
  const all = useMemo(() => (inbox.state === "ready" ? mergeLibrary(inbox.data) : []), [inbox]);
  const head = <div className="screen-head"><h1>文库</h1><span className="sub">读过、在读和待读的论文</span></div>;
  if (inbox.state === "loading") return <div className="screen-in">{head}<div className="view-msg">正在加载…</div></div>;
  if (inbox.state === "error") return <div className="screen-in">{head}<div className="view-msg">{inbox.message}</div></div>;
  const counts = countByStatus(all);
  const shown = filterLibrary(all, { status, topicId, query });
  const topics = inbox.data.topics;

  if (all.length === 0) {
    return (
      <div className="screen-in">
        {head}
        <div className="empty">
          <h2>文库还是空的</h2>
          <p>添加一篇论文，或者等课题里推荐的论文出现。读过的每一篇都会留在这里。</p>
          <button className="primary" onClick={onAdd}>添加论文</button>
        </div>
      </div>
    );
  }
  return (
    <div className="screen-in">
      {head}
      <div className="filters" role="group" aria-label="按状态筛选">
        {FILTERS.filter((f) => f === "all" || counts[f] > 0 || f === status).map((f) => (
          <button key={f} className="filter" aria-pressed={status === f} onClick={() => setStatus(f)}>
            {label(f)}<span className="n">{counts[f]}</span>
          </button>
        ))}
        {topics.length > 1 && (
          <select className="pick" aria-label="按课题筛选" value={topicId ?? ""} onChange={(e) => setTopicId(e.target.value || null)}>
            <option value="">全部课题</option>
            {topics.map((t) => <option key={t.id} value={t.id}>{t.name}</option>)}
          </select>
        )}
        <input className="field" type="search" placeholder="筛选标题、作者" aria-label="筛选标题、作者" value={query} onChange={(e) => setQuery(e.target.value)} />
      </div>
      {shown.length === 0
        ? <div className="empty"><p>没有符合条件的论文。</p></div>
        : shown.map((item) => <PaperRow key={item.paper_id} item={item} onChange={reload} />)}
      <p className="quiet">现在这里列出的是最近推荐、待读和在读的论文。</p>
    </div>
  );
}
