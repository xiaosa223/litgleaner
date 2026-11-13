// 课题调研：先预览（请求来源、不调用模型）→ 看清候选数和费用后确认 → 后台逐轮执行（第 7.5 节，docs/SURVEY.md）。
import { useCallback, useEffect, useState } from "react";
import { ApiError, missingModel, topicApi } from "../api";
import { useModelSetup } from "../ModelSetup";
import { Modal, useToast } from "../ui";
import { useAsync } from "../useAsync";
import { gapText, jobText, money, sourceLabel, pct, surveyAdvice, surveyEstimate, surveyTotals, type SurveyPreview, type SurveyRound, type SurveyStatus } from "../topicView";

const POLL_MS = 5000;
const runKey = (topicId: string) => `survey-run:${topicId}`;
// 进行中的调研编号只是本机便利：读写失败（隐私模式等）时只是回来后看不到进度
const savedRun = (topicId: string) => { try { return localStorage.getItem(runKey(topicId)); } catch { return null; } };
const saveRun = (topicId: string, runId: string | null) => {
  try { if (runId) localStorage.setItem(runKey(topicId), runId); else localStorage.removeItem(runKey(topicId)); } catch { /* 见上 */ }
};

function ConfirmSurvey({ plan, busy, error, onStart, onClose }: {
  plan: SurveyPreview; busy: boolean; error: string | null; onStart: () => void; onClose: () => void;
}) {
  return (
    <Modal title="确认开始调研" onClose={onClose} width={560}>
      {surveyEstimate(plan).map((line) => <p key={line} className="d">{line}</p>)}
      {plan.uncovered.length > 0 && (
        <div className="panel-card" style={{ marginTop: 8 }}>
          <h4>这次没有覆盖到的</h4>
          <ul>
                        {plan.uncovered.map((gap) => <li key={gap}>{gapText(gap)}</li>)}
          </ul>
        </div>
      )}
      <div className={`hint-line${error ? " bad" : ""}`} role="alert">{error}</div>
      <div className="dialog-actions">
        <button className="ghost" onClick={onClose}>取消</button>
        <button className="primary" disabled={busy || plan.candidates.length === 0} onClick={onStart}>
          {busy ? "正在提交…" : plan.candidates.length === 0 ? "没有候选论文" : "开始调研"}
        </button>
      </div>
    </Modal>
  );
}

function Rounds({ rounds }: { rounds: SurveyRound[] }) {
  const last = rounds[rounds.length - 1];
  return (
    <>
      <h2 className="block-title">每一轮</h2>
      <table className="plain">
        <thead><tr><th>轮次</th><th className="num">候选</th><th className="num">以前没见过</th><th className="num">其中相关</th><th className="num">覆盖度</th><th className="num">费用</th></tr></thead>
        <tbody>
          {rounds.map((r) => (
            <tr key={r.number}>
              <td>第 {r.number} 轮{r.cancelled ? "（已取消）" : ""}</td>
              <td className="num">{r.candidates}</td><td className="num">{r.new_papers}</td><td className="num">{r.new_passed}</td>
              <td className="num">{r.coverage === null ? "—" : pct(r.coverage)}</td><td className="num">{money(r.cost_usd)}</td>
            </tr>
          ))}
        </tbody>
      </table>
      <div className="two" style={{ marginTop: 20 }}>
        <div className="panel-card">
          <h4>最近一轮检索的来源</h4>
          {last.sources.length === 0 && <div className="tag">没有记录来源回执。</div>}
          {last.sources.map((s) => (
            <div className="src-row" key={`${s.source}-${s.mode}`}><span>{sourceLabel(s)}</span><span>{s.error ? "失败" : `${s.fetched} 篇${s.truncated ? "（有截断）" : ""}`}</span></div>
          ))}
        </div>
        <div className="panel-card">
          <h4>没有覆盖的范围</h4>
          {last.uncovered.length === 0 ? <div className="tag">没有记录到明显的缺口。</div> : <ul>{last.uncovered.map((gap) => <li key={gap}>{gapText(gap)}</li>)}</ul>}
          <div className="tag" style={{ marginTop: 6 }}>覆盖度只说明在上面这些来源里还能找到多少新论文，不代表“全了”。</div>
        </div>
      </div>
    </>
  );
}

function Summary({ rounds, running, onStart, starting }: { rounds: SurveyRound[]; running: boolean; onStart: () => void; starting: boolean }) {
  const totals = surveyTotals(rounds);
  const advice = surveyAdvice(rounds);
  const last = rounds[rounds.length - 1];
  return (
    <>
      <div className="stats">
        <div className="stat"><b>{totals.candidates}</b><span>检索到的候选论文</span></div>
        <div className="stat"><b>{totals.fresh}</b><span>新加入的论文</span></div>
        <div className="stat"><b>{totals.relevant}</b><span>其中与课题相关</span></div>
        <div className={`stat${advice?.covered ? " good" : ""}`}><b>{last.coverage === null ? "—" : pct(last.coverage)}</b><span>最近一轮里新的相关论文</span></div>
      </div>
      {advice && (
        <div className="pcard" style={{ marginTop: 12 }}>
          <div className="pcard-top">
            <div style={{ flex: 1 }}><b>{advice.title}</b><div className="pcard-by">{advice.text}</div></div>
            <button className="ghost" disabled={running || starting} onClick={onStart} title="执行前会先显示预计篇数和费用">
              {starting ? "正在检索来源…" : "再扩展一轮"}
            </button>
          </div>
        </div>
      )}
      <p className="tag">累计费用 {money(totals.cost)}。</p>
    </>
  );
}

function useSurveyRun(topicId: string, onFinished: (status: SurveyStatus) => void) {
  const [run, setRun] = useState<SurveyStatus | null>(null);
  const [runId, setRunId] = useState(() => savedRun(topicId));
  useEffect(() => {
    if (!runId) return;
    let alive = true;
    const check = () => topicApi.survey(topicId, runId).then((status) => {
      if (!alive) return;
      setRun(status);
      if (status.status !== "pending") { saveRun(topicId, null); setRunId(null); onFinished(status); }
    }, (error: unknown) => {
      // 记着的任务已不存在（换了数据库等）：忘掉它，不然按钮会一直不可用；其他错误下次再查
      if (alive && error instanceof ApiError && error.status === 404) { saveRun(topicId, null); setRunId(null); }
    });
    void check();
    const timer = window.setInterval(() => void check(), POLL_MS);
    return () => { alive = false; window.clearInterval(timer); };
  }, [topicId, runId, onFinished]);
  const track = useCallback((status: SurveyStatus) => { saveRun(topicId, status.run_id); setRun(status); setRunId(status.run_id); }, [topicId]);
  return { run, running: runId !== null, track };
}

export function SurveyTab({ topicId }: { topicId: string }) {
  const toast = useToast();
  const requireModel = useModelSetup();
  const [roundsState, reloadRounds] = useAsync(() => topicApi.rounds(topicId), [topicId]);
  const finished = useCallback((status: SurveyStatus) => { reloadRounds(); if (status.status === "done") toast("调研有新结果了"); }, [reloadRounds, toast]);
  const { run, running, track } = useSurveyRun(topicId, finished);
  const [plan, setPlan] = useState<SurveyPreview | null>(null);
  const [phase, setPhase] = useState<"idle" | "previewing" | "starting">("idle");
  const [error, setError] = useState<string | null>(null);

  const preview = async () => {
    setPhase("previewing");
    setError(null);
    try { setPlan(await topicApi.previewSurvey(topicId)); }
    catch (cause) { toast(cause instanceof Error ? cause.message : "检索来源失败"); }
    finally { setPhase("idle"); }
  };
  const start = async (chosen: SurveyPreview) => {
    setPhase("starting");
    setError(null);
    try {
      track(await topicApi.startSurvey(topicId, chosen.id));
      setPlan(null);
    } catch (cause) {
      const task = missingModel(cause);
      if (task !== null) requireModel(task || "triage", () => void start(chosen)); // 配好模型后用同一份预览继续，幂等
      else setError(cause instanceof Error ? cause.message : "提交失败");
    } finally { setPhase("idle"); }
  };

  if (roundsState.state === "loading") return <div className="view-msg">正在读取调研记录…</div>;
  if (roundsState.state === "error") return <div className="view-msg">{roundsState.message}</div>;
  const rounds = roundsState.data;
  return (
    <>
      {running && (
        <div className="pcard" role="status" style={{ marginBottom: 12 }}>
          <b>调研进行中</b>
          <div className="pcard-by">{jobText(run?.status ?? "pending", run?.last_error ?? null)}已完成 {run?.rounds.length ?? 0} 轮。新论文会陆续出现在“论文”和“阅读路线”里。</div>
        </div>
      )}
      {run && !running && run.status === "dead" && <div className="degraded" role="alert">{jobText(run.status, run.last_error)}</div>}
      {rounds.length === 0 ? (
        <div className="empty">
          <h2>还没有调研过这个课题</h2>
          <p>调研会按你写的研究背景和目标，从各个学术来源找这个方向的综述、高被引论文和近两年的论文，再顺着引用扩展。开始前会先告诉你找到多少篇、大概花多少钱，确认后才调用模型。</p>
          <button className="primary" disabled={running || phase !== "idle"} onClick={() => void preview()}>
            {phase === "previewing" ? "正在检索各个来源，可能要一两分钟…" : "开始调研"}
          </button>
        </div>
      ) : (
        <>
          <Summary rounds={rounds} running={running} starting={phase === "previewing"} onStart={() => void preview()} />
          <Rounds rounds={rounds} />
        </>
      )}
      {plan && <ConfirmSurvey plan={plan} busy={phase === "starting"} error={error} onStart={() => void start(plan)} onClose={() => setPlan(null)} />}
    </>
  );
}
