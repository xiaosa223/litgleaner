import assert from "node:assert/strict";
import { test } from "node:test";
import { countByStatus, filterLibrary, mergeLibrary, searchPapers, statusOf } from "./library.ts";
import type { Inbox, InboxItem } from "./types.ts";

const item = (id: string, over: Partial<InboxItem> = {}): InboxItem => ({
  paper_id: id, title: `Paper ${id}`, authors: ["Ada Lovelace"], venue: null, published_at: null, doi: null,
  score: null, tier_label: null, provisional: false, summary: null, topic_id: null, topic_name: null,
  pushed_at: null, reading_status: null, ...over,
});

const inbox: Inbox = {
  recommended: [item("a", { reading_status: "reading", topic_id: "t1" }), item("b", { topic_id: "t2" }), item("c", { reading_status: "read" })],
  reading: [item("a", { reading_status: "reading", topic_id: "t1" })],
  queued: [item("d", { reading_status: "queued", title: "Layer Normalization", authors: ["Jimmy Ba"] })],
  topics: [],
};

test("合并去重：同一篇只出现一次，在读排最前，其次待读", () => {
  assert.deepEqual(mergeLibrary(inbox).map((i) => i.paper_id), ["a", "d", "b", "c"]);
});

test("没有阅读状态的论文算新推荐", () => {
  assert.equal(statusOf(item("x")), "new");
  assert.equal(statusOf(item("x", { reading_status: "read" })), "read");
});

test("按状态计数", () => {
  assert.deepEqual(countByStatus(mergeLibrary(inbox)), { all: 4, new: 1, reading: 1, queued: 1, read: 1, abandoned: 0 });
});

test("按状态、课题和文字筛选，条件同时生效", () => {
  const all = mergeLibrary(inbox);
  assert.deepEqual(filterLibrary(all, { status: "reading", topicId: null, query: "" }).map((i) => i.paper_id), ["a"]);
  assert.deepEqual(filterLibrary(all, { status: "all", topicId: "t2", query: "" }).map((i) => i.paper_id), ["b"]);
  assert.deepEqual(filterLibrary(all, { status: "queued", topicId: null, query: "ba" }).map((i) => i.paper_id), ["d"]);
  assert.deepEqual(filterLibrary(all, { status: "read", topicId: "t1", query: "" }), []);
});

test("检索：不分大小写，每个词都要命中标题、作者或出处", () => {
  const all = mergeLibrary(inbox);
  assert.deepEqual(searchPapers(all, "LAYER jimmy").map((i) => i.paper_id), ["d"]);
  assert.deepEqual(searchPapers(all, "layer lovelace"), []);
  assert.deepEqual(searchPapers(all, "   "), []);
});
