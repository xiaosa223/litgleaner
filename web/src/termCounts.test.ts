import assert from "node:assert/strict";
import { test } from "node:test";
import { countBody, countFor, hitsText, rowKey, tooLong, type TermCounts } from "./termCounts.ts";

test("请求体：去掉空行和空同义词，同义词按逗号拆开", () => {
  const editor = { include: [{ term: " EEG ", synonyms: "electroencephalography，  " }, { term: " ", synonyms: "x" }], exclude: [" fMRI", ""], glossary: [] };
  assert.deepEqual(countBody(editor), { include: [{ term: "EEG", synonyms: ["electroencephalography"] }], exclude: ["fMRI"] });
  assert.equal(countBody({ ...editor, include: [] }), null);
  assert.equal(tooLong({ ...editor, exclude: ["x".repeat(121)] }), true);
  assert.equal(tooLong(editor), false);
});

test("计数结果只对应查询时的那一行：改了词或同义词就不再显示", () => {
  const counts: TermCounts = { source: "openalex", since: "2025-11-13", until: "2026-11-13", counts: [{ term: "EEG", hits: 900, error: null }] };
  const sent = new Map([[rowKey({ term: "EEG", synonyms: "a, b" }), counts.counts[0]]]);
  assert.equal(countFor(sent, { term: "eeg ", synonyms: "a，b" })?.hits, 900);
  assert.equal(countFor(sent, { term: "EEG", synonyms: "a" }), null);
});

test("篇数说明：搜不到、偏窄、合适、很宽、查询失败", () => {
  assert.match(hitsText({ term: "x", hits: 0, error: null }), /没有搜到.*排除词/);
  assert.match(hitsText({ term: "x", hits: 12, error: null }), /12 篇.*偏窄/);
  assert.match(hitsText({ term: "x", hits: 3650, error: null }), /3,650 篇.*每天约 10 篇/);
  assert.match(hitsText({ term: "x", hits: 80000, error: null }), /很宽/);
  assert.doesNotMatch(hitsText({ term: "x", hits: 80000, error: null }), /每篇都要/);
  assert.match(hitsText({ term: "x", hits: null, error: "source_unavailable" }), /稍后/);
});
