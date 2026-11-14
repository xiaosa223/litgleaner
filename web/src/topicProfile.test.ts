import assert from "node:assert/strict";
import { test } from "node:test";
import { documentError, documentProblem, markAutoDraft, profileForm, takeAutoDraft } from "./topicProfile.ts";

test("推断结果填进新建课题表单：目标一行一个", () => {
  assert.deepEqual(profileForm({ name: "课题", background: "背景", content: "内容", goals: ["一", "二"] }), {
    name: "课题", background: "背景", content: "内容", goals: "一\n二",
  });
});

test("上传前在本地挡掉不支持的格式和过大的文件", () => {
  assert.equal(documentProblem({ name: "任务书.PDF", size: 1000 }), null);
  assert.equal(documentProblem({ name: "开题报告.docx", size: 1000 }), null);
  assert.equal(documentProblem({ name: "说明.md", size: 1000 }), null);
  assert.match(documentProblem({ name: "旧版.doc", size: 1000 }) ?? "", /docx/);
  assert.match(documentProblem({ name: "a.pdf", size: 21 * 1024 * 1024 }) ?? "", /20 MB/);
  assert.match(documentProblem({ name: "a.pdf", size: 0 }) ?? "", /空/);
});

test("服务端错误码换成用户看得懂的提示", () => {
  assert.match(documentError("document_no_text"), /扫描/);
  assert.match(documentError("document_type_unsupported"), /PDF/);
  assert.match(documentError("document_too_large"), /过大/);
  assert.match(documentError("anything else"), /没能读取/);
});

test("自动起草标记只用一次；没有 sessionStorage 时不报错", () => {
  const store = new Map<string, string>();
  const storage = { getItem: (k: string) => store.get(k) ?? null, setItem: (k: string, v: string) => void store.set(k, v), removeItem: (k: string) => void store.delete(k) };
  (globalThis as { sessionStorage?: unknown }).sessionStorage = storage;
  markAutoDraft("t1");
  assert.equal(takeAutoDraft("t1"), true);
  assert.equal(takeAutoDraft("t1"), false);
  assert.equal(takeAutoDraft("other"), false);
  delete (globalThis as { sessionStorage?: unknown }).sessionStorage;
  markAutoDraft("t2");
  assert.equal(takeAutoDraft("t2"), false);
});
