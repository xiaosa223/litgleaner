import assert from "node:assert/strict";
import { test } from "node:test";
import { draftEditor, editorFrom, queryBody, queryError, reasonFor, splitList, type QueryDraft } from "./topicQuery.ts";

const draft: QueryDraft = {
  topic_id: "t", plan_hash: "p", model: "m", created_at: "2026-11-13T00:00:00Z", reading_language: "zh-CN",
  include: [{ term: "EEG foundation model", reason: "对应研究内容", synonyms: ["brain foundation model"] }],
  exclude: [{ term: "fMRI", reason: "你说不关心影像" }],
  glossary: [{ forms: ["foundation model"], translation: "基础模型", definition: "大规模预训练模型", source: null }],
  seed_titles: [], warnings: [],
};

test("从当前检索式生成编辑表：同义词按检索词归位", () => {
  const editor = editorFrom({ include: ["a", "b"], synonyms: { a: ["a1", "a2"] }, exclude: ["x"] }, []);
  assert.deepEqual(editor.include, [{ term: "a", synonyms: "a1, a2" }, { term: "b", synonyms: "" }]);
  assert.deepEqual(editor.exclude, ["x"]);
});

test("从草稿生成编辑表，并能查到每个词的理由（不分大小写）", () => {
  const editor = draftEditor(draft);
  assert.equal(editor.include[0].term, "EEG foundation model");
  assert.equal(editor.glossary[0].translation, "基础模型");
  assert.equal(reasonFor(draft, "include", "eeg FOUNDATION model"), "对应研究内容");
  assert.equal(reasonFor(draft, "exclude", "fmri"), "你说不关心影像");
  assert.equal(reasonFor(null, "include", "x"), null);
});

test("同义词可以用中英文逗号、分号或换行分隔", () => {
  assert.deepEqual(splitList("a, b，c；d\ne;; "), ["a", "b", "c", "d", "e"]);
});

test("请求体：去掉空行；没有检索词或排除词与检索词重叠时给出说明", () => {
  const editor = { include: [{ term: " a ", synonyms: "b" }, { term: "", synonyms: "" }], exclude: ["", "c"], glossary: [{ term: "", translation: "", definition: "" }] };
  const ok = queryBody(editor);
  assert.deepEqual(ok, { body: { include: [{ term: "a", synonyms: ["b"] }], exclude: ["c"], glossary: [] }, error: null });
  assert.match(queryBody({ ...editor, include: [] }).error!, /至少/);
  assert.match(queryBody({ ...editor, exclude: ["B"] }).error!, /排除词/);
});

test("术语的多个原文写法原样保存，不合并成一个字符串", () => {
  const editor = editorFrom({ include: ["a"], synonyms: {}, exclude: [] }, [{ forms: ["LLM", "large language model"], translation: "大语言模型", definition: null, source: null }]);
  assert.deepEqual(queryBody(editor).body!.glossary, [{ forms: ["LLM", "large language model"], translation: "大语言模型", definition: null }]);
});

test("只填了一半的术语不静默丢掉，提示补全", () => {
  const editor = { include: [{ term: "a", synonyms: "" }], exclude: [], glossary: [{ term: "LLM", translation: " ", definition: "" }] };
  assert.match(queryBody(editor).error!, /LLM.*译名/);
  assert.match(queryBody({ ...editor, glossary: [{ term: "", translation: "大模型", definition: "" }] }).error!, /原文/);
});

test("错误码换成中文说明，不认识的原样显示", () => {
  assert.match(queryError("topic_changed_on_disk"), /刷新/);
  assert.equal(queryError("网络断了"), "网络断了");
});
