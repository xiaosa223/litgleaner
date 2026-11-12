import assert from "node:assert/strict";
import { test } from "node:test";
import { mergeQa, qaStatementText, qaStatus, qaWaiting, selectForQa, submission, type QaView } from "./qa.ts";

const view = (id: string, state: QaView["state"] = "queued"): QaView => ({
  question: { id, paper_id: "p", text: "为什么？", selection: null, page: null, request_id: id, created_at: "2026-11-12T00:00:00Z" },
  state, answer: null, code: null,
});

test("未知提交结果的重试沿用请求 ID，修改问题、选区或页码才产生新请求", () => {
  const first = submission(" 为什么？ ", { text: "author text", page: 0 }, null);
  assert.equal(submission("为什么？", { text: "author text", page: 0 }, first), first);
  assert.equal(first.page, 0);
  for (const next of [
    submission("另外的问题？", { text: "author text", page: 0 }, first),
    submission("为什么？", { text: "other text", page: 0 }, first),
    submission("为什么？", { text: "author text", page: 1 }, first),
    submission("为什么？", null, first),
  ]) assert.notEqual(next.request_id, first.request_id);
});

test("整篇提问保留空选区与空页码，不伪造第一页", () => {
  const result = submission("问题", null, null);
  assert.equal(result.page, null);
  assert.equal(result.selection, null);
});

test("长选区完整进入请求，超限明确拒绝而不静默截断", () => {
  const text = "Original text. ".repeat(60) + "Important ending.";
  const selected = selectForQa(text, 0);
  assert.deepEqual(selected, { text, page: 0 });
  assert.equal(submission("解释结尾", selected, null).selection, text);
  assert.equal(selectForQa("a".repeat(6000), 0)?.text.length, 6000);
  assert.throws(() => selectForQa("a".repeat(6001), 0), /选区过长/);
  assert.equal(selectForQa(null, null), null);
});

test("比较多篇论文时分别保留来源标题，旧答案缺标题仍保留身份", () => {
  const text = "[[paper:a]] 与 [[paper:b]] 的差别。";
  assert.equal(qaStatementText(text, { a: "Paper A", b: "Paper B" }), "《Paper A》 与 《Paper B》 的差别。");
  assert.equal(qaStatementText(text), "《a》 与 《b》 的差别。");
  assert.equal(qaStatementText("普通陈述"), "普通陈述");
});

test("历史和提交响应乱序返回时，已完成回答不退回排队，证据原样保留", () => {
  const done: QaView = { ...view("a", "done"), answer: { id: "answer", model: "model", statements: [
    { text: "结论", inference: false, citations: [{ paper_id: "p", page: 0, quote: "Actual author text" }], note_citations: [], mentioned_paper_ids: [] },
    { text: "推断", inference: true, citations: [], note_citations: [], mentioned_paper_ids: [] },
  ] } };
  const merged = mergeQa([done, view("b")], [view("a")]);
  assert.equal(merged[0], done);
  assert.equal(merged[1].question.id, "b");
  assert.deepEqual(merged[0].answer?.statements[0].citations, done.answer?.statements[0].citations);
  assert.equal(merged[0].answer?.statements[1].inference, true);
});

test("历史更新按问题身份合并，同一时刻按 ID 稳定排列", () => {
  const pending = view("a"), failed = view("a", "failed");
  assert.deepEqual(mergeQa([view("b"), pending], [failed]).map((row) => [row.question.id, row.state]), [["a", "failed"], ["b", "queued"]]);
});

test("预算与配置等待继续只读刷新，终态停止轮询", () => {
  for (const state of ["queued", "retrying", "configuration_required", "budget_exceeded"] as const) {
    assert.equal(qaWaiting(view("a", state)), true);
    assert.ok(qaStatus(view("a", state)).length > 5);
  }
  for (const state of ["failed", "done"] as const) assert.equal(qaWaiting(view("a", state)), false);
});
