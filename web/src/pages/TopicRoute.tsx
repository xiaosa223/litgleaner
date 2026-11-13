// 阅读路线（零模型费用，docs/READING-ROUTE.md）和课题地图（先预览费用再生成，docs/TOPIC-MAP.md）。
import { useCallback, useEffect, useMemo, useState } from "react";
import { ApiError, missingModel, topicApi } from "../api";
import { useModelSetup } from "../ModelSetup";
import { navigate, paths } from "../router";
import { Modal, useToast } from "../ui";
import { useAsync } from "../useAsync";
import { jobText, money, routeGroups, type MapPreview, type MapStatus, type ReadingRoute, type RoutePaper } from "../topicView";

const POLL_MS = 4000;
const NO_PAPERS = "这个课题下还没有通过初筛的论文。先在“调研进度”里开始一轮调研，或者等每天的新论文推送。";

function RouteItem({ paper, no }: { paper: RoutePaper; no: number }) {
  const year = paper.published_at?.slice(0, 4);
  return (
    <div className="route-item">
      <span className="no">{no}</span>
      <div>
        <a className="t" href={paths.paper(paper.paper_id)} onClick={(e) => { e.preventDefault(); navigate(paths.paper(paper.paper_id)); }}>{paper.title}</a>
        <div className="pcard-by">
          {[year, paper.reading_value !== null ? `阅读价值 ${paper.reading_value}/100${paper.provisional ? "（只读了摘要，暂定）" : ""}` : null].filter(Boolean).join(" · ")}
        </div>
        {paper.goal_links.map((link) => (
          <div className="why" key={`${link.goal_id}-${link.reuse}`}>
            <span className="goal">{link.goal_id}</span> <span className="badge b-model">模型概括</span> {link.reuse}
            {/* 只有核验通过的才作为作者原话显示 */}
            {link.verified && link.quote && (
              <blockquote className="author"><span className="badge b-author">作者原话</span> “{link.quote}”{link.page !== null && <span className="tag"> · 第 {link.page + 1} 页</span>}</blockquote>
            )}
          </div>
        ))}
      </div>
      {paper.topic_citations > 0 && <span className="cited">课题内被引 {paper.topic_citations} 次</span>}
    </div>
  );
}

export function RouteTab({ topicId }: { topicId: string }) {
  const [state] = useAsync(() => topicApi.route(topicId), [topicId]);
  if (state.state === "loading") return <div className="view-msg">正在排阅读路线…</div>;
  if (state.state === "error") return <div className="view-msg">{state.message}</div>;
  const route = state.data;
  if (route.total === 0) return <div className="empty"><p>{NO_PAPERS}</p></div>;
  const groups = routeGroups(route.groups).filter((g) => g.papers.length > 0);
  return (
    <>
      <p className="tag">按下面的顺序读：先综述建立全貌，再读被引最多的奠基工作，然后是评分最高的代表作和近两年的进展。排序不调用模型。</p>
      {groups.map((group) => (
        <section key={group.kind}>
          <div className="route-head"><b>{group.label}</b><span>{group.note}</span></div>
          {group.papers.map((paper, i) => <RouteItem key={paper.paper_id} paper={paper} no={i + 1} />)}
        </section>
      ))}
      {route.ungrouped.length > 0 && (
        <details className="route-rest">
          <summary>其余 {route.ungrouped.length} 篇（不在上面几组里，或超出每组 20 篇的上限）</summary>
          {route.ungrouped.map((paper, i) => <RouteItem key={paper.paper_id} paper={paper} no={i + 1} />)}
        </details>
      )}
    </>
  );
}

function titlesOf(route: ReadingRoute | null): Map<string, string> {
  const papers = route ? [...route.groups.flatMap((g) => g.papers), ...route.ungrouped] : [];
  return new Map(papers.map((p) => [p.paper_id, p.title]));
}

function ConfirmMap({ plan, busy, error, onStart, onClose }: {
  plan: MapPreview; busy: boolean; error: string | null; onStart: () => void; onClose: () => void;
}) {
  const cost = plan.cached_map_id ? "已有相同输入的结果，直接复用，不再调用模型。"
    : plan.estimated_usd === null ? "还没有这项任务的历史费用，暂时估不出金额，通常是一次模型调用。"
      : `预计约 ${money(plan.estimated_usd)}，一次模型调用。`;
  return (
    <Modal title="生成课题地图" onClose={onClose} width={520}>
      <p className="d">把 {plan.input_paper_ids.length} 篇论文的标题、摘要和已有要点发给模型，按子方向分组（一般 5–8 组）。{cost}</p>
      {plan.warnings.map((warning) => <p key={warning} className="d tag">{warning}</p>)}
      {plan.omitted_paper_ids.length > 0 && <p className="d">有 {plan.omitted_paper_ids.length} 篇超出这次的输入上限，不会被分组，会列在“未归组”里。</p>}
      <div className={`hint-line${error ? " bad" : ""}`} role="alert">{error}</div>
      <div className="dialog-actions">
        <button className="ghost" onClick={onClose}>取消</button>
        <button className="primary" disabled={busy || plan.input_paper_ids.length === 0} onClick={onStart}>{busy ? "正在提交…" : "生成"}</button>
      </div>
    </Modal>
  );
}

const jobKey = (topicId: string) => `map-job:${topicId}`;
// 进行中的地图任务编号只是本机便利，读写失败时只是回来后看不到进度
const savedJob = (topicId: string) => { try { return localStorage.getItem(jobKey(topicId)); } catch { return null; } };
const saveJob = (topicId: string, key: string | null) => {
  try { if (key) localStorage.setItem(jobKey(topicId), key); else localStorage.removeItem(jobKey(topicId)); } catch { /* 见上 */ }
};

function useMapJob(topicId: string, onDone: () => void) {
  const [job, setJob] = useState<MapStatus | null>(null);
  const [pending, setPending] = useState(() => savedJob(topicId));
  useEffect(() => {
    if (!pending) return;
    let alive = true;
    const check = () => topicApi.mapJob(topicId, pending).then((status) => {
      if (!alive) return;
      setJob(status);
      if (status.status === "pending") return;
      saveJob(topicId, null);
      setPending(null);
      if (status.status === "done") onDone();
    }, (error: unknown) => {
      // 任务已不存在就忘掉；其他错误下次再查
      if (alive && error instanceof ApiError && error.status === 404) { saveJob(topicId, null); setPending(null); }
    });
    void check();
    const timer = window.setInterval(() => void check(), POLL_MS);
    return () => { alive = false; window.clearInterval(timer); };
  }, [topicId, pending, onDone]);
  const track = useCallback((status: MapStatus) => {
    setJob(status);
    if (status.status === "pending") { saveJob(topicId, status.job_key); setPending(status.job_key); }
  }, [topicId]);
  return { job, generating: pending !== null, track };
}

export function MapTab({ topicId }: { topicId: string }) {
  const toast = useToast();
  const requireModel = useModelSetup();
  const [viewState, reload] = useAsync(() => topicApi.map(topicId), [topicId]);
  const [routeState] = useAsync(() => topicApi.route(topicId), [topicId]);
  const titles = useMemo(() => titlesOf(routeState.state === "ready" ? routeState.data : null), [routeState]);
  const { job, generating, track } = useMapJob(topicId, reload);
  const [plan, setPlan] = useState<MapPreview | null>(null);
  const [busy, setBusy] = useState<"preview" | "start" | null>(null);
  const [error, setError] = useState<string | null>(null);

  const preview = async () => {
    setBusy("preview");
    setError(null);
    try { setPlan(await topicApi.previewMap(topicId)); }
    catch (cause) { toast(cause instanceof Error ? cause.message : "预览失败"); }
    finally { setBusy(null); }
  };
  const start = async (chosen: MapPreview) => {
    setBusy("start");
    setError(null);
    try {
      const status = await topicApi.startMap(topicId, chosen.plan_hash);
      track(status);
      setPlan(null);
      if (status.status === "done") reload();
    } catch (cause) {
      const task = missingModel(cause);
      if (task !== null) requireModel(task || "map", () => void start(chosen));
      else setError(cause instanceof Error ? cause.message : "提交失败");
    } finally { setBusy(null); }
  };

  if (viewState.state === "loading") return <div className="view-msg">正在读取课题地图…</div>;
  if (viewState.state === "error") return <div className="view-msg">{viewState.message}</div>;
  const view = viewState.data;
  const action = (label: string) => (
    <button className={view.result ? "ghost" : "primary"} disabled={busy !== null || generating || view.total_papers === 0} onClick={() => void preview()}>
      {busy === "preview" ? "正在准备…" : generating ? "正在生成…" : label}
    </button>
  );
  return (
    <>
      {generating && <div className="pcard" role="status" style={{ marginBottom: 12 }}>{jobText("pending", null)}</div>}
      {!generating && job?.status === "dead" && <div className="degraded" role="alert" style={{ marginBottom: 12 }}>{jobText(job.status, job.last_error)}</div>}
      {!view.result ? (
        <div className="empty">
          <h2>还没有课题地图</h2>
          <p>{view.total_papers === 0 ? NO_PAPERS : `课题地图把这个课题的 ${view.total_papers} 篇论文按子方向分组，每组附一句说明，帮你看清这个方向分成哪几块。生成前会先告诉你费用。`}</p>
          {action("生成课题地图")}
        </div>
      ) : (
        <>
          {(view.stale || view.new_paper_ids.length > 0) && (
            <div className="pcard" style={{ marginBottom: 12 }}>
              <div className="pcard-top">
                <div style={{ flex: 1 }} className="pcard-by">
                  {view.stale ? `这张地图已经过期：${view.stale_reasons.join("；")}。` : ""}
                  {view.new_paper_ids.length > 0 ? `有 ${view.new_paper_ids.length} 篇新论文还没归组。` : ""}
                  地图不会自动重新生成。
                </div>
                {action("重新生成")}
              </div>
            </div>
          )}
          <div className="map-grid">
            {view.result.groups.map((group) => (
              <div className="panel-card map-card" key={group.name}>
                <h4>{group.name}<span>{group.paper_ids.length} 篇</span></h4>
                <p><span className="badge b-model">模型概括</span> {group.description}</p>
                <ul>{group.paper_ids.map((id) => (
                  <li key={id}><a href={paths.paper(id)} onClick={(e) => { e.preventDefault(); navigate(paths.paper(id)); }}>{titles.get(id) ?? (routeState.state === "loading" ? "正在读取标题…" : "（标题暂时读不到，点开查看）")}</a></li>
                ))}</ul>
              </div>
            ))}
          </div>
          {view.ungrouped.length > 0 && <p className="quiet">另有 {view.ungrouped.length} 篇没有归入任何子方向。</p>}
          {view.result.warnings.map((warning) => <p key={warning} className="tag">{warning}</p>)}
          <p className="tag">由 {view.result.model} 生成于 {view.result.created_at.slice(0, 10)}。</p>
        </>
      )}
      {plan && <ConfirmMap plan={plan} busy={busy === "start"} error={error} onStart={() => void start(plan)} onClose={() => setPlan(null)} />}
    </>
  );
}
