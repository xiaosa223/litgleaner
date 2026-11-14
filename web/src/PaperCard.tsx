// 论文的两种列表样式：卡片（今天、课题）和紧凑的行（文库）。标题在前，分数和状态在后。
import { api } from "./api";
import { statusOf, type LibraryStatus } from "./library";
import { navigate, paths } from "./router";
import type { InboxItem, ReadingStatus } from "./types";
import { ModelText, useToast } from "./ui";

export const STATUS_LABEL: Record<LibraryStatus, string> = {
  new: "新推荐", queued: "待读", reading: "在读", read: "已读", abandoned: "放弃",
};

const byline = (item: InboxItem) =>
  [item.authors.slice(0, 3).join("、") + (item.authors.length > 3 ? " 等" : ""), item.venue, item.published_at?.slice(0, 4)]
    .filter(Boolean).join(" · ");

const open = (item: InboxItem) => navigate(paths.paper(item.paper_id));

function Score({ item }: { item: InboxItem }) {
  if (item.score === null) return null;
  return (
    <div className="pcard-score" title={item.provisional ? "只读了摘要，分数是临时的" : "阅读价值分"}>
      {item.tier_label && <span className="lvl">{item.tier_label}</span>}
      <span className="score">{item.score}<small className="of">/100</small></span>
      {item.provisional && !item.tier_label && <span className="provisional">暂定</span>}
    </div>
  );
}

export function PaperCard({ item, onChange, showTopic = true }: { item: InboxItem; onChange: () => void; showTopic?: boolean }) {
  const toast = useToast();
  const status = statusOf(item);
  const later = async () => {
    try {
      await api.setReading(item.paper_id, "queued");
      toast("已加入待读");
      onChange();
    } catch (error) {
      toast(error instanceof Error ? error.message : "操作失败");
    }
  };
  return (
    <article className="pcard">
      <div className="pcard-top">
        <h3 className="pcard-title"><button className="title-link" onClick={() => open(item)}>{item.title}</button></h3>
        <Score item={item} />
      </div>
      <div className="pcard-by">{byline(item)}</div>
      {item.summary && <div style={{ marginTop: 8 }}><ModelText text={item.summary} /></div>}
      <div className="pcard-actions">
        <button className="primary sm" onClick={() => open(item)}>{status === "reading" ? "继续读" : "去读"}</button>
        {status === "new" && <button className="ghost" onClick={() => void later()}>稍后读</button>}
        {status !== "new" && status !== "reading" && <span className="state">{STATUS_LABEL[status]}</span>}
        {showTopic && item.topic_name && <span className="topic-chip" style={{ marginLeft: "auto" }}>{item.topic_name}</span>}
      </div>
    </article>
  );
}

const PICKABLE: ReadingStatus[] = ["queued", "reading", "read", "abandoned"];

export function PaperRow({ item, onChange }: { item: InboxItem; onChange: () => void }) {
  const toast = useToast();
  const status = statusOf(item);
  const change = async (next: ReadingStatus) => {
    try {
      await api.setReading(item.paper_id, next);
      toast(`已标为${STATUS_LABEL[next]}`);
      onChange();
    } catch (error) {
      toast(error instanceof Error ? error.message : "操作失败");
    }
  };
  return (
    <div className="prow">
      <div>
        <div className="prow-title"><button className="title-link" onClick={() => open(item)}>{item.title}</button></div>
        <div className="pcard-by">
          {byline(item)}{item.topic_name && <> · <span className="topic-chip">{item.topic_name}</span></>}
        </div>
      </div>
      <select className="pick" aria-label="阅读状态" value={status} onChange={(e) => void change(e.target.value as ReadingStatus)}>
        {status === "new" && <option value="new" disabled>新推荐</option>}
        {PICKABLE.map((s) => <option key={s} value={s}>{STATUS_LABEL[s]}</option>)}
      </select>
      <span className="score" title="阅读价值分">{item.score ?? ""}</span>
    </div>
  );
}
