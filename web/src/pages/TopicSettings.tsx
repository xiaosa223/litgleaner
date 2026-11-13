// 课题设置：跟踪状态（启用、暂停）、研究描述、按新描述重新筛选已有论文（第 7.1、7.3、12.8 节）。
// 与命令行 topic activate / pause / backfill 等价，都不调用模型；重新筛选在确认后才排队。说明见 docs/TOPIC-DRAFT.md。
import { useEffect, useRef, useState } from "react";
import { missingModel, topicApi } from "../api";
import { useModelSetup } from "../ModelSetup";
import type { TopicSummary } from "../topicView";
import { backfillText, formFrom, isDirty, LIMITS, profileBody, settingsError, type BackfillPlan, type ProfileForm, type TopicDetail } from "../topicSettings";
import { useToast } from "../ui";
import { useAsync } from "../useAsync";

const message = (cause: unknown, fallback: string) => (cause instanceof Error ? settingsError(cause.message) : fallback);

/** 一行常驻说明：成功后的下一步写在这里，不放在一闪而过的提示里 */
function Note({ text, bad = false }: { text: string | null; bad?: boolean }) {
  return <div className={`hint-line${bad ? " bad" : ""}`} role={bad ? "alert" : "status"}>{text}</div>;
}

function Tracking({ topic, onChanged, goQuery }: { topic: TopicSummary; onChanged: () => void; goQuery: () => void }) {
  const toast = useToast();
  const [busy, setBusy] = useState(false);
  const [note, setNote] = useState<{ text: string; bad: boolean } | null>(null);
  const change = async (to: "activate" | "pause") => {
    setBusy(true); setNote(null);
    try {
      await (to === "activate" ? topicApi.activate(topic.id) : topicApi.pause(topic.id));
      toast(to === "activate" ? "已恢复跟踪" : "已暂停");
      setNote({ bad: false, text: to === "activate"
        ? "从现在起每天检索新论文。暂停期间没有检索这个课题，需要补上可以在“调研进度”里再做一轮调研。"
        : "不再检索新论文，也不花钱；已有的论文、笔记和调研结果都还在。" });
      onChanged();
    } catch (cause) { setNote({ bad: true, text: message(cause, "操作失败") }); }
    finally { setBusy(false); }
  };
  const text = topic.status === "active" ? "正在跟踪：每天检索这个课题的新论文，筛选评分后推荐给你。"
    : topic.status === "paused" ? "已暂停：不检索新论文，也不花钱。之前的论文、笔记和调研结果都保留着。"
      : topic.terms === 0 ? "还没启用。先在“检索词”里起草并保存检索词，保存时会自动启用。" : "还没启用。启用后每天检索这个课题的新论文。";
  return (
    <section className="query-block">
      <h2 className="block-title">跟踪状态</h2>
      <div className="pcard"><div className="pcard-top">
        <div className="pcard-by" style={{ flex: 1 }}>{text}</div>
        {topic.status === "active" && <button className="ghost sm" disabled={busy} onClick={() => void change("pause")}>暂停跟踪</button>}
        {topic.status !== "active" && topic.terms > 0 && <button className="primary sm" disabled={busy} onClick={() => void change("activate")}>{topic.status === "paused" ? "恢复跟踪" : "启用"}</button>}
        {topic.terms === 0 && <button className="primary sm" onClick={goQuery}>去起草检索词</button>}
      </div></div>
      <Note text={note?.text ?? null} bad={note?.bad} />
    </section>
  );
}

function Profile({ detail, onSaved }: { detail: TopicDetail; onSaved: (saved: TopicDetail, changed: boolean) => void }) {
  const toast = useToast();
  const [form, setForm] = useState<ProfileForm>(() => formFrom(detail));
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const errorLine = useRef<HTMLDivElement>(null);
  const dirty = isDirty(form, detail);
  useEffect(() => { if (error) errorLine.current?.scrollIntoView({ block: "center", behavior: "smooth" }); }, [error]);
  const set = (key: "name" | "background" | "content") => (e: { target: { value: string } }) => setForm({ ...form, [key]: e.target.value });
  const setGoal = (i: number, text: string) => setForm({ ...form, goals: form.goals.map((g, j) => (j === i ? { ...g, text } : g)) });
  const inFlight = useRef(false); // 按钮变灰前的连点也只发一次，否则新目标会被编两次号
  const save = async () => {
    const { body, error: invalid } = profileBody(form);
    if (!body) { setError(invalid); return; }
    if (inFlight.current) return;
    inFlight.current = true;
    setBusy(true); setError(null);
    try {
      const saved = await topicApi.saveProfile(detail.id, body);
      toast("已保存");
      onSaved(saved.topic, saved.changed); // 父组件换上带编号的新定义，这个表随之重建
    } catch (cause) { setError(message(cause, "保存失败")); setBusy(false); inFlight.current = false; }
  };
  return (
    <section className="query-block">
      <h2 className="block-title">研究描述</h2>
      <p className="tag">系统按这些描述筛选论文、判断和你课题的关系。改了背景、内容或目标后，之后的论文按新描述筛选；改名称不影响筛选。</p>
      <fieldset disabled={busy} style={{ border: 0, padding: 0, margin: 0, minWidth: 0 }}>
        <div className="form-row"><label htmlFor="s-name">课题名称</label>
          <input id="s-name" className="field" maxLength={LIMITS.name} value={form.name} onChange={set("name")} /></div>
        <div className="form-row"><label htmlFor="s-bg">研究背景</label>
          <textarea id="s-bg" className="field" maxLength={LIMITS.text} value={form.background} onChange={set("background")} /></div>
        <div className="form-row"><label htmlFor="s-ct">研究内容</label>
          <textarea id="s-ct" className="field" maxLength={LIMITS.text} value={form.content} onChange={set("content")} /></div>
        <div role="group" aria-labelledby="s-goals">
          <div className="block-label" id="s-goals">研究目标</div>
          {form.goals.map((goal, i) => (
            <div className="query-row" key={goal.id ?? `new-${i}`}>
              <div className="query-fields two">
                <input className="field" aria-label={`研究目标 ${i + 1}`} maxLength={LIMITS.goal} value={goal.text} onChange={(e) => setGoal(i, e.target.value)} placeholder="一个具体的目标" />
                <button className="ghost sm" aria-label={`删除研究目标 ${i + 1}`} onClick={() => setForm({ ...form, goals: form.goals.filter((_, j) => j !== i) })}>删除</button>
              </div>
            </div>
          ))}
          {form.goals.length < LIMITS.goals && <button className="ghost sm" onClick={() => setForm({ ...form, goals: [...form.goals, { id: null, text: "" }] })}>添加目标</button>}
        </div>
      </fieldset>
      <div ref={errorLine}><Note text={error} bad /></div>
      <div className="dialog-actions" style={{ justifyContent: "flex-start" }}>
        <button className="primary" disabled={busy || !dirty} onClick={() => void save()}>{busy ? "正在保存…" : "保存描述"}</button>
        {dirty && <button className="ghost" disabled={busy} onClick={() => { setForm(formFrom(detail)); setError(null); }}>放弃修改</button>}
      </div>
    </section>
  );
}

function Backfill({ topicId, changed }: { topicId: string; changed: boolean }) {
  const requireModel = useModelSetup();
  const [plan, setPlan] = useState<BackfillPlan | null>(null);
  const [busy, setBusy] = useState<"preview" | "start" | null>(null);
  const [note, setNote] = useState<{ text: string; bad: boolean } | null>(null);
  const preview = async () => {
    setBusy("preview"); setNote(null);
    try { setPlan(await topicApi.previewBackfill(topicId)); }
    catch (cause) { setNote({ bad: true, text: message(cause, "查询失败") }); }
    finally { setBusy(null); }
  };
  const start = async (chosen: BackfillPlan) => {
    setBusy("start"); setNote(null);
    try {
      const { queued } = await topicApi.startBackfill(topicId, chosen.plan);
      setPlan(null);
      setNote({ bad: false, text: queued === 0 ? "没有需要重新筛选的论文。"
        : `已排队 ${queued} 篇，后台会逐篇初筛；通过初筛的论文照常取全文和评估，之后出现在推荐里。` });
    } catch (cause) {
      const task = missingModel(cause);
      if (task !== null) requireModel(task || "triage", () => void start(chosen));
      else { setPlan(null); setNote({ bad: true, text: message(cause, "提交失败") }); }
    } finally { setBusy(null); }
  };
  return (
    <section className="query-block">
      <h2 className="block-title">重新筛选已有论文</h2>
      <p className="tag">{changed ? "研究描述刚改过。" : ""}描述或检索词改动后，系统只按新定义筛选之后发现的论文，不会自动花钱重筛文库里已有的论文。需要时在这里先看篇数和费用，确认后再筛。</p>
      {plan && <div className="pcard" role="status">{backfillText(plan)}</div>}
      <Note text={note?.text ?? null} bad={note?.bad} />
      <div className="dialog-actions" style={{ justifyContent: "flex-start" }}>
        {plan && plan.papers > 0
          ? <><button className="primary" disabled={busy !== null} onClick={() => void start(plan)}>{busy === "start" ? "正在提交…" : `确认重新筛选 ${plan.papers} 篇`}</button>
            <button className="ghost" disabled={busy !== null} onClick={() => setPlan(null)}>取消</button></>
          : <button className="ghost" disabled={busy !== null} onClick={() => void preview()}>{busy === "preview" ? "正在统计…" : "看看有多少篇要筛"}</button>}
      </div>
    </section>
  );
}

export function SettingsTab({ topic, onChanged, goQuery }: { topic: TopicSummary; onChanged: () => void; goQuery: () => void }) {
  const [loaded, retry] = useAsync(() => topicApi.detail(topic.id), [topic.id]);
  // 保存后直接用服务端返回的定义，不再请求一次；version 让编辑表和回填预览随之重建
  const [saved, setSaved] = useState<{ detail: TopicDetail; version: number; changed: boolean } | null>(null);
  if (loaded.state === "loading" && !saved) return <div className="view-msg">正在读取课题…</div>;
  if (loaded.state === "error" && !saved) {
    return <div className="view-msg">{loaded.message} <button className="ghost sm" onClick={retry}>重试</button></div>;
  }
  const detail = saved?.detail ?? (loaded.state === "ready" ? loaded.data : null);
  if (!detail) return null;
  const version = saved?.version ?? 0;
  return (
    <>
      <Tracking topic={topic} onChanged={onChanged} goQuery={goQuery} />
      <Profile key={`profile-${version}`} detail={detail} onSaved={(next, changed) => {
        setSaved((prev) => ({ detail: next, version: (prev?.version ?? 0) + 1, changed: (prev?.changed ?? false) || changed }));
        onChanged();
      }} />
      {saved?.changed && saved.detail.status === "active" && <p className="tag" role="status">之后发现的论文按新描述筛选；文库里已有的论文可以在下面按新描述重新筛选。</p>}
      {topic.status === "active" && <Backfill key={`backfill-${version}`} topicId={topic.id} changed={saved?.changed ?? false} />}
    </>
  );
}
