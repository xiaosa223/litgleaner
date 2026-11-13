// 检索词：模型起草（先预览费用再确认）→ 研究者逐条修改 → 保存并启用（第 7.2 节，docs/TOPIC-DRAFT.md）。
import { useCallback, useEffect, useRef, useState } from "react";
import { ApiError, missingModel, topicApi } from "../api";
import { useModelSetup } from "../ModelSetup";
import { money, jobText } from "../topicView";
import { draftEditor, editorFrom, queryBody, queryError, reasonFor, type DraftPreview, type DraftStatus, type DraftView, type Editor, type QueryDraft } from "../topicQuery";
import { Modal, useToast } from "../ui";
import { CountButton, Hits, useTermCounts, type TermCountState } from "./TermCounts";
import { useAsync } from "../useAsync";

const POLL_MS = 3000;
const jobKey = (topicId: string) => `draft-job:${topicId}`;
// 进行中的起草任务编号只是本机便利，读写失败时只是回来后看不到进度
const savedJob = (topicId: string) => { try { return localStorage.getItem(jobKey(topicId)); } catch { return null; } };
const saveJob = (topicId: string, key: string | null) => {
  try { if (key) localStorage.setItem(jobKey(topicId), key); else localStorage.removeItem(jobKey(topicId)); } catch { /* 见上 */ }
};

const LOST_AFTER = 3; // 连续几次查不到进度才提示，偶尔一次网络抖动不打扰

function useDraftJob(topicId: string, onDone: (draft: QueryDraft | null) => void) {
  const [job, setJob] = useState<DraftStatus | null>(null);
  const [pending, setPending] = useState(() => savedJob(topicId));
  const [failures, setFailures] = useState(0);
  useEffect(() => {
    if (!pending) return;
    let alive = true;
    let timer = 0;
    const check = () => topicApi.draftJob(topicId, pending).then((status) => {
      if (!alive) return;
      setJob(status);
      setFailures(0);
      if (status.status === "pending") { timer = window.setTimeout(check, POLL_MS); return; }
      saveJob(topicId, null);
      setPending(null);
      if (status.status === "done") onDone(status.draft);
    }, (error: unknown) => {
      if (!alive) return;
      if (error instanceof ApiError && error.status === 404) { saveJob(topicId, null); setPending(null); return; }
      setFailures((n) => n + 1);
      timer = window.setTimeout(check, POLL_MS); // 上一次请求结束后再查，慢响应不会叠在一起
    });
    void check();
    return () => { alive = false; window.clearTimeout(timer); };
  }, [topicId, pending, onDone]);
  const track = useCallback((status: DraftStatus) => {
    setJob(status);
    if (status.status === "pending") { saveJob(topicId, status.job_key); setPending(status.job_key); }
    else if (status.status === "done") onDone(status.draft);
  }, [topicId, onDone]);
  return { job, drafting: pending !== null, lost: failures >= LOST_AFTER, track };
}

function ConfirmDraft({ plan, busy, error, onStart, onClose }: {
  plan: DraftPreview; busy: boolean; error: string | null; onStart: () => void; onClose: () => void;
}) {
  const cost = plan.cached ? "这份描述已经起草过，直接复用上次的结果，不再调用模型。"
    : plan.estimated_usd === null ? "一次模型调用。还没有这项任务的历史费用，暂时估不出金额。"
      : `一次模型调用，预计约 ${money(plan.estimated_usd)}。`;
  return (
    <Modal title="起草检索词" onClose={onClose} width={520}>
      <p className="d">把你写的研究背景、内容和目标{plan.seed_titles.length > 0 ? `，以及 ${plan.seed_titles.length} 篇种子论文的标题和摘要` : ""}发给模型，起草英文检索词、排除词和术语表，每个词都附选择理由。{cost}</p>
      <p className="d">草稿不会自动生效，你可以逐条修改后再保存。</p>
      {plan.seeds_missing.length > 0 && <p className="d tag">有 {plan.seeds_missing.length} 篇种子论文还不在文库里，这次不会用到：{plan.seeds_missing.join("、")}</p>}
      <div className={`hint-line${error ? " bad" : ""}`} role="alert">{error}</div>
      <div className="dialog-actions">
        <button className="ghost" onClick={onClose}>取消</button>
        <button className="primary" disabled={busy} onClick={onStart}>{busy ? "正在提交…" : "起草"}</button>
      </div>
    </Modal>
  );
}

const Reason = ({ text }: { text: string | null }) => (text ? <div className="why"><span className="badge b-model">模型说明</span> {text}</div> : null);

function TermRows({ editor, draft, counts, onChange }: { editor: Editor; draft: QueryDraft | null; counts: TermCountState; onChange: (e: Editor) => void }) {
  const setInclude = (i: number, field: "term" | "synonyms", value: string) =>
    onChange({ ...editor, include: editor.include.map((row, j) => (j === i ? { ...row, [field]: value } : row)) });
  return (
    <section className="query-block">
      <h2 className="block-title">检索词</h2>
      <p className="tag">每天按这些词在标题和摘要里检索新论文，调研时也用它们找已有论文。同义词用逗号分隔。</p>
      {editor.include.map((row, i) => (
        <div className="query-row" key={i}>
          <div className="query-fields">
            <input className="field" aria-label={`检索词 ${i + 1}`} value={row.term} onChange={(e) => setInclude(i, "term", e.target.value)} placeholder="英文检索词" />
            <input className="field" aria-label={`检索词 ${i + 1} 的同义词`} value={row.synonyms} onChange={(e) => setInclude(i, "synonyms", e.target.value)} placeholder="同义词、缩写（可不填）" />
            <button className="ghost sm" aria-label={`删除检索词 ${i + 1}`} onClick={() => onChange({ ...editor, include: editor.include.filter((_, j) => j !== i) })}>删除</button>
          </div>
          <Reason text={reasonFor(draft, "include", row.term)} />
          <Hits item={counts.hitsFor(editor, row)} />
        </div>
      ))}
      <button className="ghost sm" onClick={() => onChange({ ...editor, include: [...editor.include, { term: "", synonyms: "" }] })}>添加检索词</button>
      <CountButton state={counts} editor={editor} />
      <h2 className="block-title">排除词</h2>
      <p className="tag">标题或摘要含这些词的论文直接跳过，不花初筛费用。拿不准就不要加，漏掉论文比多看几篇更糟。</p>
      {editor.exclude.map((term, i) => (
        <div className="query-row" key={i}>
          <div className="query-fields two">
            <input className="field" aria-label={`排除词 ${i + 1}`} value={term} onChange={(e) => onChange({ ...editor, exclude: editor.exclude.map((t, j) => (j === i ? e.target.value : t)) })} />
            <button className="ghost sm" aria-label={`删除排除词 ${i + 1}`} onClick={() => onChange({ ...editor, exclude: editor.exclude.filter((_, j) => j !== i) })}>删除</button>
          </div>
          <Reason text={reasonFor(draft, "exclude", term)} />
        </div>
      ))}
      <button className="ghost sm" onClick={() => onChange({ ...editor, exclude: [...editor.exclude, ""] })}>添加排除词</button>
    </section>
  );
}

function GlossaryRows({ editor, onChange }: { editor: Editor; onChange: (e: Editor) => void }) {
  const set = (i: number, field: "term" | "translation" | "definition", value: string) =>
    onChange({ ...editor, glossary: editor.glossary.map((row, j) => (j === i ? { ...row, [field]: value } : row)) });
  return (
    <section className="query-block">
      <h2 className="block-title">术语表</h2>
      <p className="tag">翻译和讲解统一用这里的译名。一个术语有几种英文写法时用逗号分隔。</p>
      {editor.glossary.map((row, i) => (
        <div className="query-row" key={i}>
          <div className="query-fields glossary">
            <input className="field" aria-label={`术语 ${i + 1} 的原文写法`} value={row.term} onChange={(e) => set(i, "term", e.target.value)} placeholder="原文写法" />
            <input className="field" aria-label={`术语 ${i + 1} 的译名`} value={row.translation} onChange={(e) => set(i, "translation", e.target.value)} placeholder="译名" />
            <input className="field" aria-label={`术语 ${i + 1} 的释义`} value={row.definition} onChange={(e) => set(i, "definition", e.target.value)} placeholder="一句话释义（可不填）" />
            <button className="ghost sm" aria-label={`删除术语 ${i + 1}`} onClick={() => onChange({ ...editor, glossary: editor.glossary.filter((_, j) => j !== i) })}>删除</button>
          </div>
        </div>
      ))}
      <button className="ghost sm" onClick={() => onChange({ ...editor, glossary: [...editor.glossary, { term: "", translation: "", definition: "" }] })}>添加术语</button>
    </section>
  );
}

function initialEditor(view: DraftView): Editor {
  // 还没有检索词时直接用草稿；已有检索词时保留现状，由“用草稿替换”显式切换
  return view.draft && !view.stale && view.current.include.length === 0 ? draftEditor(view.draft) : editorFrom(view.current, view.glossary);
}

const SAVED: Record<string, string> = {
  active: "已保存。之后发现的新论文按新的检索词处理。",
  paused: "已保存。这个课题仍是暂停状态，不会检索新论文。",
  new: "已保存。",
};

export function QueryTab({ topicId, active, onSaved }: { topicId: string; active: boolean; onSaved: (status: string) => void }) {
  const toast = useToast();
  const requireModel = useModelSetup();
  const [viewState, reload] = useAsync(() => topicApi.draft(topicId), [topicId]);
  const [editor, setEditor] = useState<Editor | null>(null);
  const [dirty, setDirty] = useState(false);
  const [plan, setPlan] = useState<DraftPreview | null>(null);
  const [busy, setBusy] = useState<"preview" | "start" | "save" | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [startError, setStartError] = useState<string | null>(null);
  const errorLine = useRef<HTMLDivElement>(null);
  const dirtyRef = useRef(dirty);
  dirtyRef.current = dirty;
  const drafted = useCallback((draft: QueryDraft | null) => {
    // 研究者已经改过的内容不覆盖，留给“用草稿替换”
    if (draft && !dirtyRef.current) setEditor(draftEditor(draft));
    else if (draft) toast("草稿已生成。你改过的内容还在，点“用草稿替换”可以换成草稿。");
    reload();
  }, [reload, toast]);
  const { job, drafting, lost, track } = useDraftJob(topicId, drafted);
  const counts = useTermCounts(topicId);
  useEffect(() => { if (viewState.state === "ready" && editor === null) setEditor(initialEditor(viewState.data)); }, [viewState, editor]);
  useEffect(() => { if (error) errorLine.current?.scrollIntoView({ block: "center", behavior: "smooth" }); }, [error]);
  const edit = (next: Editor) => { setEditor(next); setDirty(true); };

  const preview = async () => {
    setBusy("preview"); setError(null);
    try { setPlan(await topicApi.previewDraft(topicId)); }
    catch (cause) { setError(cause instanceof Error ? queryError(cause.message) : "预览失败"); }
    finally { setBusy(null); }
  };
  const start = async (chosen: DraftPreview) => {
    setBusy("start"); setStartError(null);
    try { track(await topicApi.startDraft(topicId, chosen.plan_hash)); setPlan(null); }
    catch (cause) {
      const task = missingModel(cause);
      if (task !== null) requireModel(task || "draft", () => void start(chosen));
      else setStartError(cause instanceof Error ? queryError(cause.message) : "提交失败");
    } finally { setBusy(null); }
  };
  const save = async () => {
    if (!editor) return;
    const { body, error: invalid } = queryBody(editor);
    if (!body) { setError(invalid); return; }
    setBusy("save"); setError(null);
    try {
      const saved = await topicApi.saveQuery(topicId, body);
      toast(!active && saved.status === "active" ? "已保存并启用。之后每天会检索这个课题的新论文，可以在“调研进度”里找已有的论文。" : SAVED[saved.status] ?? "已保存。");
      setDirty(false); reload(); onSaved(saved.status);
    } catch (cause) {
      setError(cause instanceof Error ? queryError(cause.message) : "保存失败");
    } finally { setBusy(null); }
  };

  if (viewState.state === "loading" || (viewState.state === "ready" && editor === null)) return <div className="view-msg">正在读取检索词…</div>;
  if (viewState.state === "error") return <div className="view-msg">{viewState.message}</div>;
  const view = viewState.data;
  const draft = view.draft && !view.stale ? view.draft : null;
  const empty = view.current.include.length === 0;
  return (
    <>
      {drafting && <div className="pcard" role="status" style={{ marginBottom: 12 }}>{lost ? "暂时查不到起草进度，检查拾穗是否还在运行；恢复后会自动继续。" : `正在起草检索词。${jobText("pending", null)}`}</div>}
      {!drafting && job?.status === "dead" && <div className="degraded" role="alert" style={{ marginBottom: 12 }}>{jobText(job.status, job.last_error)}</div>}
      <div className="pcard" style={{ marginBottom: 16 }}>
        <div className="pcard-top">
          <div style={{ flex: 1 }} className="pcard-by">
            {empty && !draft ? "这个课题还没有检索词。不熟悉检索式也没关系：让模型根据你写的研究背景、内容和目标起草，每个词都会说明为什么选它，你再逐条修改。"
              : view.stale ? "研究描述改过了，旧草稿是按原来的描述写的，建议重新起草。"
                : draft ? `草稿由 ${draft.model} 起草于 ${draft.created_at.slice(0, 10)}。` : "可以直接修改下面的检索词，或让模型重新起草。"}
          </div>
          {draft && (!empty || dirty) && <button className="ghost sm" onClick={() => { setEditor(draftEditor(draft)); setDirty(true); }}>用草稿替换</button>}
          <button className={empty && !draft ? "primary" : "ghost"} disabled={busy !== null || drafting} onClick={() => void preview()}>
            {busy === "preview" ? "正在准备…" : drafting ? "正在起草…" : empty && !draft ? "让模型起草" : "重新起草"}
          </button>
        </div>
        {draft?.warnings.map((w) => <p key={w} className="tag">{w}</p>)}
      </div>
      {editor && <TermRows editor={editor} draft={draft} counts={counts} onChange={edit} />}
      {editor && <GlossaryRows editor={editor} onChange={edit} />}
      <div ref={errorLine} className={`hint-line${error ? " bad" : ""}`} role="alert">{error}</div>
      <div className="dialog-actions" style={{ justifyContent: "flex-start" }}>
        <button className="primary" disabled={busy !== null || !editor} onClick={() => void save()}>{busy === "save" ? "正在保存…" : active ? "保存" : "保存并启用"}</button>
        {!active && <span className="tag">启用后每天检索这个课题的新论文，初筛按你配置的模型计费，受每日预算限制。</span>}
      </div>
      {plan && <ConfirmDraft plan={plan} busy={busy === "start"} error={startError} onStart={() => void start(plan)} onClose={() => { setPlan(null); setStartError(null); }} />}
    </>
  );
}
