// 课题：列表、新建，以及每个课题一页（调研进度、阅读路线、课题地图、论文）。第 7.5、12.8 节。
import { useEffect, useState } from "react";
import { api, isNotConnected, NOT_CONNECTED } from "../api";
import { mergeLibrary } from "../library";
import { PaperCard } from "../PaperCard";
import { usePreview } from "../preview/usePreview";
import { navigate, paths } from "../router";
import type { Inbox, TopicProfile } from "../types";
import { Modal, useToast } from "../ui";
import type { Async } from "../useAsync";
import { MapTab, RouteTab, SurveyTab } from "./TopicSurvey";

function NewTopic({ onClose }: { onClose: () => void }) {
  const [form, setForm] = useState({ name: "", background: "", content: "", goals: "" });
  const [message, setMessage] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);
  const goals = form.goals.split("\n").map((g) => g.trim()).filter(Boolean);
  const ready = form.name.trim() !== "" && goals.length > 0;
  const set = (key: keyof typeof form) => (e: { target: { value: string } }) => setForm({ ...form, [key]: e.target.value });
  const submit = async () => {
    setBusy(true);
    setMessage(null);
    try {
      const created = await api.createTopic({ name: form.name.trim(), background: form.background.trim(), content: form.content.trim(), goals });
      onClose();
      navigate(paths.topic(created.id));
    } catch (error) {
      setMessage(isNotConnected(error) ? NOT_CONNECTED : error instanceof Error ? error.message : "创建失败");
    } finally {
      setBusy(false);
    }
  };
  return (
    <Modal onClose={onClose} title="新建课题" width={560}>
      <p className="tag" style={{ margin: "-6px 0 12px", fontSize: 12 }}>用你自己的话写就行。系统会据此起草检索式，并逐个解释为什么选这些词。</p>
      <div className="form-row"><label htmlFor="t-name">课题名称</label>
        <input id="t-name" className="field" autoFocus value={form.name} onChange={set("name")} placeholder="例如：脑电基础模型" /></div>
      <div className="form-row"><label htmlFor="t-bg">研究背景</label>
        <textarea id="t-bg" className="field" value={form.background} onChange={set("background")} placeholder="这个领域现在是什么情况，要解决的问题为什么重要" /></div>
      <div className="form-row"><label htmlFor="t-ct">研究内容</label>
        <textarea id="t-ct" className="field" value={form.content} onChange={set("content")} placeholder="打算研究什么、用什么方法；关心什么、不关心什么" /></div>
      <div className="form-row"><label htmlFor="t-goals">研究目标（一行一个）</label>
        <textarea id="t-goals" className="field" value={form.goals} onChange={set("goals")} placeholder={"例如：找到适合小样本的预训练方法\n比较不同通道排布下的迁移效果"} /></div>
      <div className={`hint-line${message ? " bad" : ""}`} role="status">{message}</div>
      <div className="dialog-actions">
        <button className="ghost" onClick={onClose}>取消</button>
        <button className="primary" disabled={!ready || busy} onClick={() => void submit()}>{busy ? "正在创建…" : "创建课题"}</button>
      </div>
    </Modal>
  );
}

function Goals({ topic }: { topic: TopicProfile }) {
  return (
    <ul className="goals">
      {topic.goals.map((goal) => <li key={goal.id}><span className="goal">{goal.id}</span> {goal.text}</li>)}
    </ul>
  );
}

export function Topics({ inbox }: { inbox: Async<Inbox> }) {
  const [creating, setCreating] = useState(window.location.hash === "#new");
  useEffect(() => {
    const onHash = () => window.location.hash === "#new" && setCreating(true);
    window.addEventListener("popstate", onHash);
    return () => window.removeEventListener("popstate", onHash);
  }, []);
  const head = (
    <div className="screen-head">
      <h1>课题</h1><span className="sub">调研一个方向，并持续跟踪新论文</span>
      <button className="primary sm end" onClick={() => setCreating(true)}>新建课题</button>
    </div>
  );
  const close = () => {
    setCreating(false);
    if (window.location.hash) window.history.replaceState(window.history.state, "", paths.topics);
  };
  const dialog = creating && <NewTopic onClose={close} />;
  if (inbox.state === "loading") return <div className="screen-in">{head}<div className="view-msg">正在加载…</div>{dialog}</div>;
  if (inbox.state === "error") return <div className="screen-in">{head}<div className="view-msg">{inbox.message}</div>{dialog}</div>;
  const papers = mergeLibrary(inbox.data);
  return (
    <div className="screen-in">
      {head}
      {inbox.data.topics.length === 0 && (
        <div className="empty">
          <h2>还没有课题</h2>
          <p>写下研究背景、内容和目标，系统会找这个方向已有的论文，告诉你先读哪几篇，之后每天跟踪新论文。</p>
          <button className="primary" onClick={() => setCreating(true)}>新建课题</button>
        </div>
      )}
      {inbox.data.topics.map((topic) => (
        <button key={topic.id} className="pcard topic-card" onClick={() => navigate(paths.topic(topic.id))}>
          <div className="pcard-top">
            <h3>{topic.name}</h3>
            <span className="pcard-by" style={{ marginLeft: "auto" }}>{papers.filter((p) => p.topic_id === topic.id).length} 篇论文</span>
          </div>
          <Goals topic={topic} />
        </button>
      ))}
      {dialog}
    </div>
  );
}

type Tab = "survey" | "route" | "map" | "papers";

export function TopicPage({ topicId, inbox, reload }: { topicId: string; inbox: Async<Inbox>; reload: () => void }) {
  const [preview] = usePreview();
  const toast = useToast();
  const [chosen, setTab] = useState<Tab | null>(null);
  const crumb = <div className="crumb"><a onClick={() => navigate(paths.topics)}>课题</a> /</div>;
  if (inbox.state === "loading") return <div className="screen-in">{crumb}<div className="view-msg">正在加载…</div></div>;
  if (inbox.state === "error") return <div className="screen-in">{crumb}<div className="view-msg">{inbox.message}</div></div>;
  const topic = inbox.data.topics.find((t) => t.id === topicId);
  if (!topic) {
    return <div className="screen-in">{crumb}<div className="empty"><h2>没有找到这个课题</h2><p>它可能已经暂停或改名。</p></div></div>;
  }
  const papers = mergeLibrary(inbox.data).filter((p) => p.topic_id === topic.id);
  const tabs: [Tab, string][] = preview
    ? [["survey", "调研进度"], ["route", "阅读路线"], ["map", "课题地图"], ["papers", `论文 ${papers.length}`]]
    : [["papers", `论文 ${papers.length}`]];
  const tab = chosen && tabs.some(([key]) => key === chosen) ? chosen : tabs[0][0];
  const expand = async () => {
    try {
      await api.startSurvey(topic.id);
      toast("已开始新一轮调研");
    } catch (error) {
      toast(isNotConnected(error) ? NOT_CONNECTED : error instanceof Error ? error.message : "操作失败");
    }
  };
  return (
    <div className="screen-in">
      {crumb}
      <div className="screen-head" style={{ marginBottom: 4 }}><h1>{topic.name}</h1></div>
      <Goals topic={topic} />
      <div className="subtabs" role="tablist">
        {tabs.map(([key, text]) => <button key={key} role="tab" aria-selected={tab === key} onClick={() => setTab(key)}>{text}</button>)}
      </div>
      {tab === "survey" && <SurveyTab onExpand={() => void expand()} />}
      {tab === "route" && <RouteTab />}
      {tab === "map" && <MapTab />}
      {tab === "papers" && (papers.length === 0
        ? <div className="empty"><p>这个课题下还没有推荐的论文。</p></div>
        : papers.map((item) => <PaperCard key={item.paper_id} item={item} onChange={reload} showTopic={false} />))}
    </div>
  );
}
