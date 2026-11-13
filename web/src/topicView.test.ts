import assert from "node:assert/strict";
import { test } from "node:test";
import { gapText, money, routeGroups, sourceLabel, surveyAdvice, surveyEstimate, surveyTotals, type SurveyPreview, type SurveyRound } from "./topicView.ts";

const round = (over: Partial<SurveyRound> = {}): SurveyRound => ({
  number: 1, run_id: "r", iteration: 1, candidates: 100, new_papers: 80, passed: 30, new_passed: 20, undetermined: 0,
  failed_jobs: [], cost_usd: 0.12, coverage: 0.2, next_recommended: true, sources: [], uncovered: [], cancelled: false,
  started_at: "2026-11-13T00:00:00Z", finished_at: "2026-11-13T00:10:00Z", ...over,
});

const preview = (over: Partial<SurveyPreview> = {}): SurveyPreview => ({
  id: "p1", candidates: new Array(120).fill({}), known_papers: 15, triage_calls: 105, fulltext_candidates: 40,
  extract_calls: 40, assess_calls: 40, uncovered: [], sources: [], estimated_usd: 0.3, estimated_pipeline_usd: 1.25,
  settings: { rounds: 2 }, ...over,
});

test("各轮累计：新入库和其中相关的论文", () => {
  const totals = surveyTotals([round(), round({ number: 2, candidates: 50, new_papers: 10, new_passed: 2, cost_usd: 0.05 })]);
  assert.deepEqual(totals, { candidates: 150, fresh: 90, relevant: 22, cost: 0.17 });
});

test("还没调研过：没有建议", () => {
  assert.equal(surveyAdvice([]), null);
});

test("最近一轮新增很少：已基本覆盖", () => {
  const advice = surveyAdvice([round({ coverage: 0.02, next_recommended: false, new_passed: 2 })]);
  assert.equal(advice?.covered, true);
  assert.match(advice!.title, /基本覆盖/);
});

test("最近一轮还在出新论文：建议再扩展", () => {
  const advice = surveyAdvice([round()]);
  assert.equal(advice?.covered, false);
  assert.match(advice!.text, /20 篇/);
});

test("初筛没做完或已取消：不给覆盖度结论", () => {
  assert.match(surveyAdvice([round({ coverage: null, next_recommended: null, undetermined: 5 })])!.text, /还有 5 篇/);
  assert.match(surveyAdvice([round({ cancelled: true, coverage: null, next_recommended: null })])!.title, /取消/);
});

test("调研前说明：候选、已有论文、模型调用、预计费用、轮数", () => {
  const lines = surveyEstimate(preview());
  const text = lines.join("\n");
  assert.match(text, /120 篇候选/);
  assert.match(text, /15 篇已在文库/);
  assert.match(text, /105 次/);
  assert.match(text, /\$1\.25/);
  assert.match(text, /2 轮/);
});

test("没有历史费用时说明估不出，不写成免费", () => {
  const text = surveyEstimate(preview({ estimated_usd: null, estimated_pipeline_usd: null })).join("\n");
  assert.match(text, /估不出/);
  assert.doesNotMatch(text, /\$0/);
});

test("金额：很小的数不显示成 0", () => {
  assert.equal(money(0), "$0");
  assert.equal(money(0.004), "不到 $0.01");
  assert.equal(money(1.234), "$1.23");
});

test("阅读路线按固定顺序给出中文分组和说明", () => {
  const groups = routeGroups([
    { kind: "recent", papers: [] },
    { kind: "reviews", papers: [] },
  ]);
  assert.deepEqual(groups.map((g) => g.kind), ["reviews", "recent"]);
  assert.ok(groups.every((g) => g.label && g.note));
});

test("没有候选论文：不列费用，说明原因和下一步", () => {
  const text = surveyEstimate(preview({ candidates: [], triage_calls: 0, extract_calls: 0, assess_calls: 0, estimated_usd: 0, estimated_pipeline_usd: 0,
    uncovered: ["reviews 没有启用支持该能力的来源"] })).join("\n");
  assert.match(text, /没有找到候选论文/);
  assert.match(text, /OpenAlex/);
  assert.doesNotMatch(text, /次调用|\$/); // 不列调用次数和金额
});

test("缺口说明里的内部代号换成中文", () => {
  assert.equal(gapText("highly_cited 没有启用支持该能力的来源"), "高被引检索：没有启用支持这项检索的来源");
  assert.equal(gapText("openalex/recent 查询失败：timeout"), "openalex 的近两年检索查询失败：timeout");
  assert.equal(gapText("部分种子 DOI 无效，未发起对应查询"), "部分种子 DOI 无效，未发起对应查询");
});

test("来源数为 0 时不出现病句", () => {
  assert.doesNotMatch(surveyEstimate(preview({ sources: [] })).join(""), /从\s*已启用的\s*个/);
});

test("最近一轮没有候选：说没找到新论文，不说等初筛", () => {
  const advice = surveyAdvice([round(), round({ number: 2, candidates: 0, new_papers: 0, passed: 0, new_passed: 0, coverage: null, next_recommended: null })]);
  assert.match(advice!.title, /没有找到新/);
  assert.doesNotMatch(advice!.text, /初筛/);
});

test("来源回执带上检索方式", () => {
  assert.equal(sourceLabel({ source: "openalex", mode: "citations", fetched: 0, truncated: false, error: null }), "openalex · 被引扩展");
});

test("第一轮全部复用：说明不再调用模型，费用来自后面几轮", () => {
  const text = surveyEstimate(preview({ known_papers: 120, triage_calls: 0, extract_calls: 0, assess_calls: 0, estimated_usd: 0.02, estimated_pipeline_usd: 0.06 })).join("\n");
  assert.match(text, /复用/);
  assert.doesNotMatch(text, /约 0 次/);
  assert.match(text, /后面几轮/);
});

test("第一轮就没有候选：不说“顺着前面的论文”", () => {
  const advice = surveyAdvice([round({ candidates: 0, new_papers: 0, passed: 0, new_passed: 0, coverage: null, next_recommended: null })]);
  assert.doesNotMatch(advice!.text, /前面/);
});
