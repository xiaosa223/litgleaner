import assert from "node:assert/strict";
import { test } from "node:test";
import { answerNote, provenLevel, relationLabel, costHint, dueLabel, guideWaiting, lengthHint, translationOf, type GuidePage } from "./guidance.ts";

const page = (job: GuidePage["view"]["job"], cardState: GuidePage["cards"][number]["evaluation_state"] = "none"): GuidePage => ({
  view: { paper_id: "p", guide: null, stale: false, job, unavailable: null, plan: null },
  cards: [{ question_index: 0, card: null, revisions: 0, evaluation: null, evaluation_state: cardState }],
  evaluate_configured: true,
  evaluate_estimated_usd: null,
});

test("导读或任何一张卡片的评价在排队时继续轮询", () => {
  assert.equal(guideWaiting(null), false);
  assert.equal(guideWaiting(page("none")), false);
  assert.equal(guideWaiting(page("queued")), true);
  assert.equal(guideWaiting(page("none", "retrying")), true);
  assert.equal(guideWaiting(page("failed", "budget_exceeded")), false);
});

test("费用提示区分未知、极小和常规金额", () => {
  assert.equal(costHint(null), "会调用一次模型");
  assert.equal(costHint(0.0004), "费用不到 $0.001");
  assert.equal(costHint(0.004), "约 $0.004");
  assert.equal(costHint(0.12), "约 $0.12");
});

test("译名取中文别名，与原名相同时不重复", () => {
  const summary = {
    concept: { id: "c", name: "Attention" },
    aliases: [{ name: "Attention", language: "en" }, { name: "注意力", language: "zh" }],
    papers: 1, definition: null,
  };
  assert.equal(translationOf(summary), "注意力");
  assert.equal(translationOf({ ...summary, aliases: [] }), "");
});

test("回答过长时提醒压缩，短回答不打扰", () => {
  assert.equal(lengthHint(""), null);
  assert.equal(lengthHint("它用查询和键算权重。"), null);
  assert.ok(lengthHint("一。二。三。四。五。六。"));
});

test("复习时间按天显示", () => {
  const now = new Date("2026-11-14T08:00:00Z");
  assert.equal(dueLabel(null, now), "");
  assert.equal(dueLabel("2026-11-14T07:00:00Z", now), "今天复习");
  assert.equal(dueLabel("2026-11-15T07:00:00Z", now), "明天复习");
  assert.equal(dueLabel("2026-11-20T08:00:00Z", now), "6 天后复习");
});

test("理解偏了的旧回答带明确提醒，对的和未评价的不加", () => {
  assert.match(answerNote("misunderstood") ?? "", /理解偏了/);
  assert.match(answerNote("missing") ?? "", /漏了/);
  assert.equal(answerNote("correct"), null);
  assert.equal(answerNote(null), null);
  assert.match(answerNote("correct", "partial") ?? "", /别处不准确/);
});

test("评价能证明的等级与后端规则一致：整题答对的对比、迁移题才到能应用", () => {
  assert.equal(provenLevel("understand", "correct", "correct"), 2);
  assert.equal(provenLevel("compare", "correct", "correct"), 3);
  assert.equal(provenLevel("transfer", "correct", "partial"), 2);
  assert.equal(provenLevel("understand", "misunderstood", "correct"), 1);
});

test("前置关系按方向读，相似和对比不分方向", () => {
  const base = { link_id: "l", concept_id: "c", name: "Softmax", reason: "", confirmed: false };
  assert.equal(relationLabel({ ...base, kind: "prerequisite", direction: "out" }), "先懂");
  assert.equal(relationLabel({ ...base, kind: "prerequisite", direction: "in" }), "是它的前置");
  assert.equal(relationLabel({ ...base, kind: "contrast", direction: "in" }), "对比");
});
