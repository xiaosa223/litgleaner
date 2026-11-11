// 全局检索：一个搜索框查论文和页面（第 12.8 节）。笔记、全文和概念的检索要等后端的检索接口；
// 开启界面预览时把示例概念也列出来，用来看结果分组的样子。
import { useMemo, useState, type KeyboardEvent } from "react";
import { searchPapers } from "./library";
import { SAMPLE_CONCEPTS } from "./preview/sample";
import { usePreview } from "./preview/usePreview";
import { navigate, paths } from "./router";
import type { InboxItem } from "./types";
import { Modal } from "./ui";

interface Hit { key: string; group: string; title: string; meta: string; go: string }

const PAGES: Hit[] = [
  ["今天", paths.today], ["课题", paths.topics], ["文库", paths.library], ["知识库", paths.knowledge], ["设置", paths.settings],
].map(([title, go]) => ({ key: `page-${go}`, group: "页面", title, meta: "", go }));
const PAPER_LIMIT = 8;

export function SearchPalette({ papers, onClose }: { papers: InboxItem[]; onClose: () => void }) {
  const [preview] = usePreview();
  const [query, setQuery] = useState("");
  const [active, setActive] = useState(0);
  const hits = useMemo<Hit[]>(() => {
    const q = query.trim().toLowerCase();
    if (!q) return PAGES;
    const found: Hit[] = searchPapers(papers, q).slice(0, PAPER_LIMIT).map((p) => ({
      key: `paper-${p.paper_id}`, group: "论文", title: p.title,
      meta: [p.authors[0], p.published_at?.slice(0, 4)].filter(Boolean).join(" · "), go: paths.paper(p.paper_id),
    }));
    const concepts: Hit[] = !preview ? [] : SAMPLE_CONCEPTS
      .filter((c) => `${c.name} ${c.translation}`.toLowerCase().includes(q))
      .map((c) => ({ key: `concept-${c.id}`, group: "概念（示例数据）", title: `${c.name} ${c.translation}`, meta: "", go: paths.knowledge }));
    return [...found, ...concepts, ...PAGES.filter((p) => p.title.includes(query.trim()))];
  }, [query, papers, preview]);
  const index = Math.min(active, Math.max(hits.length - 1, 0));

  const open = (hit: Hit | undefined) => {
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
      <input autoFocus type="search" aria-label="搜索" placeholder="搜论文标题、作者，或输入页面名" value={query}
        onChange={(e) => { setQuery(e.target.value); setActive(0); }} onKeyDown={onKey} />
      <div className="pal-list" role="listbox" aria-label="搜索结果">
        {hits.length === 0 && <div className="empty" style={{ padding: 24 }}><p>没有找到“{query.trim()}”。</p></div>}
        {hits.map((hit, i) => (
          <div key={hit.key}>
            {(i === 0 || hits[i - 1].group !== hit.group) && <div className="pal-group">{hit.group}</div>}
            <button className="pal-item" role="option" aria-selected={i === index} onMouseEnter={() => setActive(i)} onClick={() => open(hit)}>
              <span className="t">{hit.title}</span><span className="m">{hit.meta}</span>
            </button>
          </div>
        ))}
      </div>
      <div className="pal-foot"><span>↑↓ 选择</span><span>Enter 打开</span><span>Esc 关闭</span><span style={{ marginLeft: "auto" }}>范围：论文标题、作者、出处</span></div>
    </Modal>
  );
}
