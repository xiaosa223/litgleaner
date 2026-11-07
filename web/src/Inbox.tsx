// 收件箱：研究画像 + 推荐卡片。和飞书卡片是同一份推荐，卡片上点“去读”会直接打开对应论文。
import { useState } from "react";
import { api } from "./api";
import { navigate, paperPath } from "./router";
import type { InboxItem, ReadingStatus } from "./types";
import { useToast } from "./ui";
import { useAsync } from "./useAsync";

const STATUS: Partial<Record<ReadingStatus, string>> = { queued: "在待读里", reading: "在读", read: "已读", abandoned: "已放弃" };

function Card({ item, onChange }: { item: InboxItem; onChange: () => void }) {
  const toast = useToast();
  const later = () => api.setReading(item.paper_id, "queued").then(() => { toast("已加入待读"); onChange(); }, (e: Error) => toast(e.message));
  const venue = [item.venue, item.published_at].filter(Boolean).join(" · ");
  return (
    <div className="card">
      <div className="row">
        {item.score !== null && <span className="score">{item.score}</span>}
        {item.tier_label && <span className="lvl">{item.tier_label}</span>}
        {item.topic_name ? <span>{item.topic_name}</span> : <span>未评分</span>}
        {venue && <span>{venue}</span>}
        {item.reading_status && <span className="state">{STATUS[item.reading_status]}</span>}
      </div>
      <h3>{item.title}</h3>
      {item.authors.length > 0 && <div className="tag">{item.authors.slice(0, 4).join("、")}{item.authors.length > 4 ? " 等" : ""}</div>}
      {item.summary && (
        <div className="model" style={{ margin: "6px 0" }}><span className="badge b-model">模型概括</span> {item.summary}</div>
      )}
      {item.provisional && <div className="tag">只读了摘要，分数是临时的</div>}
      <div className="row">
        <button className="primary" onClick={() => navigate(paperPath(item.paper_id))}>{item.reading_status === "reading" ? "继续读" : "去读"}</button>
        {!item.reading_status && <button className="ghost" onClick={() => void later()}>稍后读</button>}
      </div>
    </div>
  );
}

export function Inbox() {
  const [inbox, reload] = useAsync(() => api.inbox(), []);
  const [chosen, setTab] = useState<"recommended" | "queued" | null>(null);
  if (inbox.state === "loading") return <div className="view-msg">正在加载…</div>;
  if (inbox.state === "error") return <div className="view-msg">{inbox.message}</div>;
  const { recommended, queued, topics } = inbox.data;
  // 没有推荐、但有待读时，默认打开待读
  const tab = chosen ?? (recommended.length === 0 && queued.length > 0 ? "queued" : "recommended");
  const list = tab === "recommended" ? recommended : queued;
  return (
    <div style={{ overflow: "auto", flex: 1 }}>
      <div className="inbox">
        <h1>推荐</h1>
        <div className="tag">从飞书卡片点“去读”会直接打开对应论文；这里是同一份推荐的本地收件箱。</div>
        {topics.map((topic) => (
          <div className="profile" key={topic.id}>
            <b>课题：</b>{topic.name}
            <ul style={{ paddingLeft: 18, margin: "6px 0 0" }}>
              {topic.goals.map((goal) => <li key={goal.id}><span className="goal">{goal.id}</span> {goal.text}</li>)}
            </ul>
          </div>
        ))}
        {topics.length === 0 && <div className="profile">还没有启用中的研究画像。运行 <code>litgleaner topic activate &lt;id&gt;</code> 启用。</div>}
        <div className="inbox-tabs" role="tablist">
          <button className="ghost" role="tab" aria-selected={tab === "recommended"} onClick={() => setTab("recommended")}>最近推荐 {recommended.length}</button>
          <button className="ghost" role="tab" aria-selected={tab === "queued"} onClick={() => setTab("queued")}>待读 {queued.length}</button>
        </div>
        {list.length === 0 && (
          <div className="empty-note">{tab === "recommended" ? "还没有推荐。流程运行并到了推送时间后，推荐会出现在这里。" : "还没有待读的论文。推荐里点“稍后读”会出现在这里。"}</div>
        )}
        {list.map((item) => <Card key={item.paper_id} item={item} onChange={reload} />)}
      </div>
    </div>
  );
}
