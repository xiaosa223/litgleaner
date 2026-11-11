// 文库查询由服务端完成，已读、放弃和只有笔记的论文都保留在这里（第 12.8 节）。
import { useEffect, useState } from "react";
import { api } from "../api";
import type { StatusFilter } from "../library";
import { PaperRow, STATUS_LABEL } from "../PaperCard";
import type { LibraryItem, LibraryPage } from "../types";

const FILTERS: StatusFilter[] = ["all", "reading", "queued", "new", "read", "abandoned"];
const label = (status: StatusFilter) => (status === "all" ? "全部" : STATUS_LABEL[status]);
const PAGE_SIZE = 50;

export function Library({ onAdd }: { onAdd: () => void }) {
  const [filters, setFilters] = useState({ status: "all" as StatusFilter, topic_id: "", q: "" });
  const [offset, setOffset] = useState(0);
  const [version, setVersion] = useState(0);
  const [items, setItems] = useState<LibraryItem[]>([]);
  const [page, setPage] = useState<LibraryPage | null>(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    const controller = new AbortController();
    let alive = true;
    setLoading(true);
    setError(null);
    if (offset === 0) setItems([]);
    const timer = window.setTimeout(() => {
      api.library({ ...filters, limit: PAGE_SIZE, offset }, controller.signal).then((result) => {
        if (!alive) return;
        setPage(result);
        setItems((previous) => offset === 0 ? result.items : [...previous, ...result.items.filter((item) => !previous.some((old) => old.paper_id === item.paper_id))]);
        setLoading(false);
      }, (reason: unknown) => {
        if (!alive) return;
        setError(reason instanceof Error ? reason.message : "文库加载失败");
        setLoading(false);
      });
    }, filters.q ? 180 : 0);
    return () => { alive = false; controller.abort(); window.clearTimeout(timer); };
  }, [filters, offset, version]);

  const change = (next: Partial<typeof filters>) => {
    setFilters((current) => ({ ...current, ...next }));
    setOffset(0);
  };
  const reload = () => { setOffset(0); setVersion((v) => v + 1); };
  const topics = page?.topics ?? [];

  return (
    <div className="screen-in">
      <div className="screen-head"><h1>文库</h1><span className="sub">读过、在读和待读的论文</span></div>
      <div className="filters" role="group" aria-label="按状态筛选">
        {FILTERS.map((status) => (
          <button key={status} className="filter" aria-pressed={filters.status === status} onClick={() => change({ status })}>
            {label(status)}{page && <span className="n">{page.counts[status]}</span>}
          </button>
        ))}
        {topics.length > 0 && (
          <select className="pick" aria-label="按课题筛选" value={filters.topic_id} onChange={(e) => change({ topic_id: e.target.value })}>
            <option value="">全部课题</option>
            {topics.map((topic) => <option key={topic.id} value={topic.id}>{topic.name}</option>)}
          </select>
        )}
        <input className="field" type="search" placeholder="筛选标题、作者、出处、DOI" aria-label="筛选文库" maxLength={300}
          value={filters.q} onChange={(e) => change({ q: e.target.value })} />
      </div>
      {error && <div className="view-msg" role="alert">{error} <button onClick={() => setVersion((v) => v + 1)}>重试</button></div>}
      {loading && items.length === 0 && <div className="view-msg" role="status">正在加载…</div>}
      {!loading && !error && items.length === 0 && (
        <div className="empty">
          <h2>{filters.status === "all" && !filters.topic_id && !filters.q ? "文库还是空的" : "没有符合条件的论文"}</h2>
          <p>添加的论文、阅读记录、笔记和高亮都会留在文库里。</p>
          <button className="primary" onClick={onAdd}>添加论文</button>
        </div>
      )}
      {items.map((item) => <PaperRow key={item.paper_id} item={item} onChange={reload} />)}
      {page && items.length > 0 && <p className="quiet">已显示 {items.length} / {page.total} 篇</p>}
      {page && items.length < page.total && !error && (
        <button className="ghost" disabled={loading} onClick={() => setOffset((value) => value + PAGE_SIZE)}>
          {loading ? "正在加载…" : "加载更多"}
        </button>
      )}
    </div>
  );
}
