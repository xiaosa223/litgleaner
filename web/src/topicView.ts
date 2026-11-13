// 课题页：调研轮次、调研前的费用说明、阅读路线、课题地图（第 7.5 节）。
// 只做数据到界面文字的转换，纯函数，可单测。接口说明见 docs/SURVEY.md、READING-ROUTE.md、TOPIC-MAP.md。

/** GET /api/topics 的一项：所有课题，包括还没启用的。 */
export interface TopicSummary {
  id: string; name: string; goals: { id: string; text: string }[];
  status: "active" | "paused" | "new"; terms: number;
}

export function topicStatus(topic: Pick<TopicSummary, "status" | "terms">): { label: string | null; hint: string | null } {
  const label = topic.status === "new" ? "未启用" : topic.status === "paused" ? "已暂停" : null;
  return { label, hint: topic.terms === 0 ? "还没有检索词" : null };
}

export interface SurveySource { source: string; mode: string; fetched: number; truncated: boolean; error: string | null }

export interface SurveyRound {
  number: number; run_id: string; iteration: number; started_at: string; finished_at: string;
  candidates: number; new_papers: number; passed: number; new_passed: number; undetermined: number;
  failed_jobs: string[]; cost_usd: number; coverage: number | null; next_recommended: boolean | null;
  sources: SurveySource[]; uncovered: string[]; cancelled: boolean;
}

export interface SurveyPreview {
  id: string; candidates: unknown[]; known_papers: number; triage_calls: number; fulltext_candidates: number;
  extract_calls: number; assess_calls: number; uncovered: string[]; sources: SurveySource[];
  estimated_usd: number | null; estimated_pipeline_usd: number | null; settings: { rounds: number };
}

export interface SurveyStatus { run_id: string; status: "pending" | "done" | "dead"; rounds: SurveyRound[]; last_error: string | null }

export type RouteKind = "reviews" | "foundations" | "representatives" | "recent";

export interface GoalLink { goal_id: string; reuse: string; quote: string | null; page: number | null; verified: boolean }

export interface RoutePaper {
  paper_id: string; title: string; published_at: string | null; publication_types: string[];
  topic_citations: number; reading_value: number | null; provisional: boolean | null; goal_links: GoalLink[];
}

export interface ReadingRoute { topic_id: string; groups: { kind: RouteKind; papers: RoutePaper[] }[]; ungrouped: RoutePaper[]; total: number }

export interface MapGroup { name: string; description: string; paper_ids: string[] }

export interface TopicMapResult { id: string; created_at: string; model: string; groups: MapGroup[]; ungrouped: string[]; warnings: string[] }

export interface MapView {
  topic_id: string; result: TopicMapResult | null; total_papers: number; new_paper_ids: string[];
  ungrouped: string[]; stale: boolean; stale_reasons: string[];
}

export interface MapPreview {
  plan_hash: string; total_papers: number; input_paper_ids: string[]; omitted_paper_ids: string[];
  estimated_usd: number | null; cached_map_id: string | null; warnings: string[];
}

export interface MapStatus { job_key: string; status: "pending" | "done" | "dead"; result: TopicMapResult | null; cost_usd: number; last_error: string | null }

export function money(value: number): string {
  if (value === 0) return "$0";
  if (value < 0.01) return "不到 $0.01";
  return `$${value.toFixed(2)}`;
}

export const pct = (value: number) => `${(value * 100).toFixed(1)}%`;

export function surveyTotals(rounds: SurveyRound[]) {
  const sum = (pick: (r: SurveyRound) => number) => rounds.reduce((total, r) => total + pick(r), 0);
  return {
    candidates: sum((r) => r.candidates),
    fresh: sum((r) => r.new_papers),
    relevant: sum((r) => r.new_passed),
    cost: Math.round(sum((r) => r.cost_usd) * 1e6) / 1e6,
  };
}

/** 根据最近一轮给出下一步建议；覆盖度只描述已检索来源里的新增，不代表“全了”。 */
export function surveyAdvice(rounds: SurveyRound[]): { title: string; text: string; covered: boolean } | null {
  const last = rounds[rounds.length - 1];
  if (!last) return null;
  if (last.cancelled) {
    return { title: "上一轮调研已取消", text: "课题设置或模型配置在调研中途改变了，已发生的费用已记录。可以重新开始一轮。", covered: false };
  }
  if (last.candidates === 0) {
    return { title: "最近一轮没有找到新的候选论文", text: last.number === 1 ? "第一轮没有找到候选论文。可以把研究背景和目标写得更具体，或换几个关键词后再调研。" : `第 ${last.number} 轮顺着前面的论文继续找，没有找到新的候选。可以先读已有的论文，需要时再扩展。`, covered: false };
  }
  if (last.coverage === null || last.next_recommended === null) {
    const left = last.undetermined > 0 ? `还有 ${last.undetermined} 篇没有完成初筛，` : "";
    return { title: "暂时算不出覆盖度", text: `第 ${last.number} 轮${left}等初筛完成后再看是否需要继续扩展。`, covered: false };
  }
  if (!last.next_recommended) {
    return {
      title: "已基本覆盖",
      text: `第 ${last.number} 轮的 ${last.candidates} 篇候选里，只有 ${last.new_passed} 篇是以前没见过的相关论文。再扩展一轮找不到多少新论文。`,
      covered: true,
    };
  }
  return {
    title: "建议再扩展一轮",
    text: `第 ${last.number} 轮还找到了 ${last.new_passed} 篇新的相关论文，顺着它们的引用可能还有。`,
    covered: false,
  };
}

const MODES: Record<string, string> = {
  reviews: "综述检索", highly_cited: "高被引检索", recent: "近两年检索", references: "参考文献扩展", citations: "被引扩展",
};

/** 后端的缺口说明带检索方式的内部代号（如 highly_cited），换成中文。 */
export function gapText(gap: string): string {
  const missing = /^(\w+) 没有启用支持该能力的来源$/.exec(gap);
  if (missing && MODES[missing[1]]) return `${MODES[missing[1]]}：没有启用支持这项检索的来源`;
  return gap.replace(/^([\w.-]+)\/(\w+) /, (whole, source: string, mode: string) => (MODES[mode] ? `${source} 的${MODES[mode]}` : whole));
}

export const sourceLabel = (s: SurveySource) => (MODES[s.mode] ? `${s.source} · ${MODES[s.mode]}` : s.source);

function emptyReason(preview: SurveyPreview): string {
  if (preview.uncovered.some((gap) => gap.includes("没有启用支持该能力的来源"))) {
    return "现在启用的来源都不支持按课题检索历史论文。需要在来源配置里启用 OpenAlex 或 Semantic Scholar（免费，不需要 key），见 docs/SOURCES.md。";
  }
  if (preview.uncovered.some((gap) => gap.includes("关键词"))) return "这个课题还没有检索关键词，补上关键词后再试。";
  if (preview.sources.some((s) => s.error)) return "有来源查询失败，可能是网络问题，稍后再试。";
  return "可以把研究背景和目标写得更具体一些，或者换几个关键词后再试。";
}

/** 确认调研前给新手看的说明：找到什么、要调用几次模型、大概花多少钱。 */
export function surveyEstimate(preview: SurveyPreview): string[] {
  if (preview.candidates.length === 0) return ["这次没有找到候选论文，不会调用模型，也不会产生费用。", emptyReason(preview)];
  const known = preview.known_papers > 0 ? `，其中 ${preview.known_papers} 篇已在文库里` : "";
  const from = preview.sources.length > 0 ? `从 ${new Set(preview.sources.map((s) => s.source)).size} 个来源` : "";
  const lines = [`${from}找到 ${preview.candidates.length} 篇候选论文${known}。`];
  const calls = preview.triage_calls + preview.extract_calls + preview.assess_calls;
  lines.push(calls === 0
    ? "这些论文之前都处理过，第一轮直接复用已有结果，不再调用模型。"
    : `模型先读标题和摘要做初筛，约 ${preview.triage_calls} 次调用；通过的论文再取全文、提取要点和评估（约 ${preview.extract_calls} + ${preview.assess_calls} 次）。`);
  const total = preview.estimated_pipeline_usd;
  if (total === null) {
    lines.push("还没有这些任务的历史费用，暂时估不出金额。实际花费受每日预算限制，可以先做一轮看看。");
  } else {
    const triage = preview.estimated_usd === null ? "" : `，其中初筛 ${money(preview.estimated_usd)}`;
    const later = preview.settings.rounds > 1 ? "（含后面几轮按候选上限估的费用）" : "";
    lines.push(`预计约 ${money(total)}${triage}${later}。这是估算，不是上限；实际花费受每日预算限制。`);
  }
  if (preview.settings.rounds > 1) {
    lines.push(`确认后会自动做 ${preview.settings.rounds} 轮：后面几轮从前一轮的高分论文顺着引用继续找。`);
  }
  return lines;
}

const ROUTE_GROUPS: Record<RouteKind, { label: string; note: string }> = {
  reviews: { label: "先读综述", note: "来源标注为综述的论文，先了解这个方向的全貌" },
  foundations: { label: "奠基工作", note: "本课题的论文引用最多的论文" },
  representatives: { label: "代表作", note: "按阅读价值评分从高到低" },
  recent: { label: "近两年进展", note: "最近两年发表的论文，从新到旧" },
};

const ROUTE_ORDER: RouteKind[] = ["reviews", "foundations", "representatives", "recent"];

export function routeGroups<T extends { kind: RouteKind }>(groups: T[]): (T & { label: string; note: string })[] {
  return ROUTE_ORDER.flatMap((kind) => groups.filter((g) => g.kind === kind).map((g) => ({ ...g, ...ROUTE_GROUPS[kind] })));
}

export function jobText(status: "pending" | "done" | "dead", error: string | null): string {
  if (status === "pending") return "正在后台进行，可以离开这个页面，回来再看。";
  if (status === "dead") return `没有完成：${error ?? "多次重试后仍然失败"}`;
  return "已完成。";
}
