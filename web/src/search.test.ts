import assert from "node:assert/strict";
import { test } from "node:test";
import { paletteHits } from "./search.ts";
import type { SearchHit } from "./types.ts";

const hit = (kind: SearchHit["kind"], extra: Partial<SearchHit> = {}): SearchHit => ({
  kind, id: `${kind}-1`, paper_id: "p1", title: "Attention", text: "片段", page: null, authorship: "author", ...extra,
});

test("检索结果按类型分组，同类保持服务端相关性次序", () => {
  const shown = paletteHits([hit("note", { authorship: "user" }), hit("paper"), hit("fulltext"), hit("note", { id: "note-2" })]);
  assert.deepEqual(shown.map((item) => item.group), ["论文", "全文", "笔记", "笔记"]);
  assert.deepEqual(shown.slice(2).map((item) => item.key), ["p1:note:note-1", "p1:note:note-2"]);
});

test("不同论文相同块编号、不同类型相同编号各有独立结果键", () => {
  const shown = paletteHits([
    hit("fulltext", { id: "0", paper_id: "p1" }),
    hit("fulltext", { id: "0", paper_id: "p2" }),
    hit("note", { id: "0", paper_id: "p1", authorship: "user" }),
  ]);
  assert.equal(new Set(shown.map((item) => item.key)).size, 3);
  assert.deepEqual(shown.map((item) => item.go), ["/papers/p1", "/papers/p2", "/papers/p1"]);
});

test("用户笔记和模型摘要保留作者身份，原文命中保留页码链接", () => {
  const [found] = paletteHits([hit("highlight", { page: 0, authorship: "user" })]);
  assert.equal(found.go, "/papers/p1?page=0");
  assert.equal(found.meta, "我的记录 · 第 1 页 · 片段");
  assert.match(paletteHits([hit("extraction", { authorship: "model" })])[0].meta, /^模型生成/);
});

test("问答检索区分用户提问和模型回答，并跳回对应页", () => {
  const shown = paletteHits([
    hit("qa", { id: "question", authorship: "user" }),
    hit("qa", { id: "answer", authorship: "model", page: 2 }),
  ]);
  assert.equal(shown[0].group, "问答");
  assert.match(shown[0].meta, /^我的记录/);
  assert.match(shown[1].meta, /^模型生成/);
  assert.equal(shown[1].go, "/papers/p1?page=2");
});

test("概念检索显示原文来源，并按出现页跳转", () => {
  const [found] = paletteHits([hit("concept", { page: 1 })]);
  assert.equal(found.group, "概念");
  assert.match(found.meta, /^作者原文/);
  assert.equal(found.go, "/papers/p1?page=1");
});
