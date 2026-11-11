import assert from "node:assert/strict";
import { test } from "node:test";
import { parseRoute, paths } from "./routes.ts";

test("首页和未知地址都落到今天", () => {
  assert.deepEqual(parseRoute("/"), { name: "today" });
  assert.deepEqual(parseRoute("/nope/x"), { name: "today" });
});

test("各页面地址", () => {
  assert.deepEqual(parseRoute("/topics"), { name: "topics" });
  assert.deepEqual(parseRoute("/topics/"), { name: "topics" });
  assert.deepEqual(parseRoute("/library"), { name: "library" });
  assert.deepEqual(parseRoute("/knowledge"), { name: "knowledge" });
  assert.deepEqual(parseRoute("/settings"), { name: "settings" });
});

test("带编号的地址解码编号，生成的地址能解析回来", () => {
  assert.deepEqual(parseRoute("/papers/01ABC"), { name: "paper", paperId: "01ABC" });
  assert.deepEqual(parseRoute(paths.paper("a/b c")), { name: "paper", paperId: "a/b c" });
  assert.deepEqual(parseRoute(paths.topic("eeg 基础")), { name: "topic", topicId: "eeg 基础" });
});

test("编号里有坏的转义时不抛错，回到今天", () => {
  assert.deepEqual(parseRoute("/papers/%E0%A4%A"), { name: "today" });
});
