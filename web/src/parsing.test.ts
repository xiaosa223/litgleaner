import assert from "node:assert/strict";
import { test } from "node:test";
import { parseChanged, parseNote } from "./parsing.ts";

const paper = (has_pdf: boolean, has_document: boolean, fulltext_status: string | null) => ({ has_pdf, has_document, fulltext_status });

test("PDF 到了但还没识别段落：提示稍等并继续检查", () => {
  const note = parseNote(paper(true, false, "available"));
  assert.equal(note?.waiting, true);
  assert.match(note!.text, /识别/);
});

test("识别失败：说明哪些功能仍可用，不再检查", () => {
  const note = parseNote(paper(true, false, "failed"));
  assert.equal(note?.waiting, false);
  assert.match(note!.text, /高亮/);
});

test("已识别、没有 PDF 时不提示", () => {
  assert.equal(parseNote(paper(true, true, "parsed")), null);
  assert.equal(parseNote(paper(false, false, null)), null);
});

test("等待超时：不再说“不到一分钟”，提示稍后刷新", () => {
  const note = parseNote(paper(true, false, "available"), true);
  assert.equal(note?.waiting, false);
  assert.doesNotMatch(note!.text, /不到一分钟/);
  assert.match(note!.text, /刷新/);
});

test("状态有变化才需要刷新阅读页", () => {
  const before = paper(true, false, "available");
  assert.equal(parseChanged(before, paper(true, false, "available")), false);
  assert.equal(parseChanged(before, paper(true, true, "parsed")), true);
  assert.equal(parseChanged(before, paper(true, false, "failed")), true);
});
