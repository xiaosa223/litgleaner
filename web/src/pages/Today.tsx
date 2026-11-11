// 今天：现在该做什么。继续读 → 今日复习 → 新推荐 → 待读；什么都没有时给出两个入口（第 12.8、15.5 节）。
import { Compass, FileText } from "lucide-react";
import { PaperCard } from "../PaperCard";
import { SAMPLE_CONCEPTS } from "../preview/sample";
import { usePreview } from "../preview/usePreview";
import { navigate, paths } from "../router";
import type { Inbox } from "../types";
import type { Async } from "../useAsync";

const NEW_LIMIT = 5;
const QUEUE_LIMIT = 3;

function Welcome({ onAdd }: { onAdd: () => void }) {
  return (
    <>
      <p className="sub" style={{ color: "var(--muted)", marginTop: -8 }}>从一篇论文开始，或者从一个课题开始。</p>
      <div className="welcome">
        <button className="choice" onClick={onAdd}>
          <span className="ico"><FileText size={19} aria-hidden /></span>
          <b>读一篇论文</b>
          <span>贴 DOI、arXiv 编号或链接，或者把 PDF 拖进来。不用先做任何设置。</span>
        </button>
        <button className="choice" onClick={() => navigate(`${paths.topics}#new`)}>
          <span className="ico"><Compass size={19} aria-hidden /></span>
          <b>调研一个课题</b>
          <span>写下研究背景和目标，系统帮你找这个方向的论文，告诉你先读哪几篇。</span>
        </button>
      </div>
    </>
  );
}

export function Today({ inbox, reload, onAdd }: { inbox: Async<Inbox>; reload: () => void; onAdd: () => void }) {
  const [preview] = usePreview();
  const date = new Date().toLocaleDateString("zh-CN", { month: "long", day: "numeric", weekday: "long" });
  const head = <div className="screen-head"><h1>今天</h1><span className="sub">{date}</span></div>;
  if (inbox.state === "loading") return <div className="screen-in">{head}<div className="view-msg">正在加载…</div></div>;
  if (inbox.state === "error") return <div className="screen-in">{head}<div className="view-msg">{inbox.message}</div></div>;
  const { reading, queued, recommended } = inbox.data;
  const fresh = recommended.filter((item) => !item.reading_status);
  const due = preview ? SAMPLE_CONCEPTS.filter((c) => c.due) : [];
  const nothing = reading.length + queued.length + recommended.length === 0;

  return (
    <div className="screen-in">
      {head}
      {nothing && <Welcome onAdd={onAdd} />}
      {reading.length > 0 && (
        <section>
          <h2 className="block-title">继续读</h2>
          {reading.map((item) => <PaperCard key={item.paper_id} item={item} onChange={reload} />)}
        </section>
      )}
      {due.length > 0 && (
        <section>
          <h2 className="block-title">今日复习 <span className="topic-chip">示例数据</span></h2>
          <div className="pcard">
            <div className="pcard-top">
              <div className="pcard-title" style={{ font: "600 15px var(--sans)" }}>{due.length} 个概念到了复习时间</div>
            </div>
            <div className="pcard-by">{due.map((c) => c.translation).join("、")}</div>
            <div className="pcard-actions">
              <button className="primary sm" onClick={() => navigate(`${paths.knowledge}#review`)}>开始复习</button>
              <span className="tag">从你自己写过的回答出发，自评不花模型费用</span>
            </div>
          </div>
        </section>
      )}
      {fresh.length > 0 && (
        <section>
          <h2 className="block-title">
            新推荐 {fresh.length}
            {fresh.length > NEW_LIMIT && <a className="more" onClick={() => navigate(paths.library)}>全部</a>}
          </h2>
          {fresh.slice(0, NEW_LIMIT).map((item) => <PaperCard key={item.paper_id} item={item} onChange={reload} />)}
        </section>
      )}
      {queued.length > 0 && (
        <section>
          <h2 className="block-title">
            待读 {queued.length}
            {queued.length > QUEUE_LIMIT && <a className="more" onClick={() => navigate(paths.library)}>全部</a>}
          </h2>
          {queued.slice(0, QUEUE_LIMIT).map((item) => <PaperCard key={item.paper_id} item={item} onChange={reload} />)}
        </section>
      )}
      {!nothing && fresh.length === 0 && reading.length === 0 && queued.length === 0 && (
        <div className="empty"><h2>今天没有新的要读</h2><p>最近的推荐都已经处理过了，可以去文库里翻翻以前读过的。</p></div>
      )}
    </div>
  );
}
