import assert from "node:assert/strict";
import { test } from "node:test";
import { backfillText, formFrom, isDirty, profileBody, settingsError, type TopicDetail } from "./topicSettings.ts";

const detail: TopicDetail = {
  id: "t", name: "脑电", background: "背景", content: "内容",
  goals: [{ id: "g1", text: "目标一" }, { id: "g2", text: "目标二" }], status: "active", terms: 3,
};

test("编辑表来自课题定义，没改动时不算脏", () => {
  const form = formFrom(detail);
  assert.deepEqual(form.goals, [{ id: "g1", text: "目标一" }, { id: "g2", text: "目标二" }]);
  assert.equal(isDirty(form, detail), false);
  assert.equal(isDirty({ ...form, content: "内容 " }, detail), false); // 只差首尾空白
  assert.equal(isDirty({ ...form, goals: [...form.goals, { id: null, text: "" }] }, detail), false);
  assert.equal(isDirty({ ...form, goals: form.goals.slice(1) }, detail), true);
});

test("请求体：保留已有目标编号，新目标不带编号，空目标丢掉", () => {
  const form = { ...formFrom(detail), name: " 新名 ", goals: [{ id: "g2", text: " 二 " }, { id: null, text: "新" }, { id: null, text: " " }] };
  assert.deepEqual(profileBody(form), {
    body: { name: "新名", background: "背景", content: "内容", goals: [{ id: "g2", text: "二" }, { id: null, text: "新" }] },
    error: null,
  });
});

test("缺项时说明缺什么", () => {
  assert.match(profileBody({ ...formFrom(detail), background: " " }).error!, /研究背景/);
  assert.match(profileBody({ ...formFrom(detail), goals: [{ id: null, text: "" }] }).error!, /目标/);
});

test("超长在本地拦下，说清上限，不等服务端回 422", () => {
  assert.match(profileBody({ ...formFrom(detail), name: "名".repeat(101) }).error!, /100/);
  assert.match(profileBody({ ...formFrom(detail), goals: Array.from({ length: 21 }, (_, i) => ({ id: null, text: `目标${i}` })) }).error!, /20/);
  assert.match(profileBody({ ...formFrom(detail), goals: [{ id: null, text: "长".repeat(1001) }] }).error!, /1000/);
  assert.match(profileBody({ ...formFrom(detail), content: "长".repeat(8001) }).error!, /8000/);
});

test("错误码换成能照着做的话", () => {
  assert.match(settingsError("topic_changed_on_disk"), /刷新/);
  assert.match(settingsError("topic_query_missing"), /检索词/);
  assert.match(settingsError("topic_invalid"), /刷新/);
  assert.equal(settingsError("别的"), "别的");
});

test("回填说明：没有要筛的、费用未知、有估价", () => {
  assert.match(backfillText({ papers: 0, estimated_usd: null, plan: "p" }), /都已/);
  assert.match(backfillText({ papers: 3, estimated_usd: null, plan: "p" }), /3 篇.*估不出/);
  assert.match(backfillText({ papers: 3, estimated_usd: 0.5, plan: "p" }), /\$0\.50/);
});
