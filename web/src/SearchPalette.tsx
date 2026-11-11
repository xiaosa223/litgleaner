// 全局检索走服务端 FTS5：论文、全文、笔记、高亮和提取摘要；页面入口始终可查。
import { useEffect, useState, type KeyboardEvent } from "react";
import { api } from "./api";
import { navigate, paths } from "./router";
import { paletteHits, type PaletteHit } from "./search";
import { Modal } from "./ui";

const PAGES: PaletteHit[] = [
  ["今天", paths.today], ["课题", paths.topics], ["文库", paths.library], ["知识库", paths.knowledge], ["设置", paths.settings],
].map(([title, go]) => ({ key: `page-${go}`, group: "页面", title, meta: "", go }));

type SearchState = { query: string; hits: PaletteHit[] } & (
  | { state: "loading" | "ready" }
  | { state: "error"; message: string }
);

export function SearchPalette({ onClose }: { onClose: () => void }) {
  const [query, setQuery] = useState("");
  const [active, setActive] = useState(0);
  const [result, setResult] = useState<SearchState>({ state: "ready", query: "", hits: [] });
  const q = query.trim();

  useEffect(() => {
    if (!q) return;
    const controller = new AbortController();
    let alive = true;
    setResult({ state: "loading", query: q, hits: [] });
    const timer = window.setTimeout(() => {
      api.search(q, controller.signal).then((data) => {
        if (alive) setResult({ state: "ready", query: q, hits: paletteHits(data.hits) });
      }, (error: unknown) => {
        if (alive) setResult({ state: "error", query: q, hits: [], message: error instanceof Error ? error.message : "检索失败" });
      });
    }, 180);
    return () => { alive = false; controller.abort(); window.clearTimeout(timer); };
  }, [q]);

  const current = q && result.query === q ? result : null;
  const loading = Boolean(q) && (!current || current.state === "loading");
  const hits = q ? [...(current?.hits ?? []), ...PAGES.filter((page) => page.title.includes(q))] : PAGES;
  const index = Math.min(active, Math.max(hits.length - 1, 0));
  const open = (hit: PaletteHit | undefined) => {
    if (!hit) return;
    onClose();
    navigate(hit.go);
  };
  const onKey = (event: KeyboardEvent) => {
    if (event.key === "ArrowDown") { event.preventDefault(); setActive((index + 1) % Math.max(hits.length, 1)); }
    if (event.key === "ArrowUp") { event.preventDefault(); setActive((index - 1 + hits.length) % Math.max(hits.length, 1)); }
    if (event.key === "Enter") open(hits[index]);
  };

  return (
    <Modal onClose={onClose} title="搜索" width={600} top bare>
      <input autoFocus type="search" aria-label="搜索" placeholder="搜论文、全文、笔记，或输入页面名" maxLength={300} value={query}
        onChange={(e) => { setQuery(e.target.value); setActive(0); }} onKeyDown={onKey} />
      <div className="pal-list" role="listbox" aria-label="搜索结果" aria-busy={loading}>
        {loading && <div className="empty" role="status" style={{ padding: 24 }}><p>正在检索…</p></div>}
        {current?.state === "error" && <div className="empty" role="alert" style={{ padding: 24 }}><p>{current.message}</p></div>}
        {!loading && current?.state !== "error" && hits.length === 0 && <div className="empty" style={{ padding: 24 }}><p>没有找到“{q}”。</p></div>}
        {hits.map((hit, i) => (
          <div key={hit.key}>
            {(i === 0 || hits[i - 1].group !== hit.group) && <div className="pal-group">{hit.group}</div>}
            <button className="pal-item" role="option" aria-selected={i === index} onMouseEnter={() => setActive(i)} onClick={() => open(hit)}>
              <span className="t">{hit.title}</span><span className="m">{hit.meta}</span>
            </button>
          </div>
        ))}
      </div>
      <div className="pal-foot"><span>↑↓ 选择</span><span>Enter 打开</span><span>Esc 关闭</span><span style={{ marginLeft: "auto" }}>范围：论文、全文、笔记、高亮、提取摘要</span></div>
    </Modal>
  );
}
