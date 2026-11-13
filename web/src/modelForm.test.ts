import assert from "node:assert/strict";
import { test } from "node:test";
import { applyPreset, probeCeiling, emptyForm, formFromSettings, probeText, taskSummary, toBody, type ModelSettingsView } from "./modelForm.ts";

const filled = () => ({
  ...applyPreset(emptyForm(), "deepseek"),
  model: " deepseek-chat ",
  apiKey: " sk-test-1234 ",
  inputPrice: "0.3",
  outputPrice: "1.2",
});

test("预设只填协议、地址和结构化方式，价格必须用户按官网填写", () => {
  const form = applyPreset(emptyForm(), "deepseek");
  assert.equal(form.protocol, "openai_chat");
  assert.equal(form.baseUrl, "https://api.deepseek.com/v1");
  assert.equal(form.structured, "prompt");
  assert.equal(form.inputPrice, "");
  assert.equal(form.model, "deepseek-chat");
  assert.equal(applyPreset(form, "custom").model, "");
  const result = toBody({ ...form, model: "deepseek-chat", apiKey: "k" });
  assert.equal(result.ok, false);
  if (!result.ok) assert.match(result.error, /价格/);
});

test("合法表单转成接口请求体：去空白、数字化，默认所有任务用同一个模型", () => {
  const result = toBody(filled());
  assert.ok(result.ok);
  if (!result.ok) return;
  assert.equal(result.body.provider, "deepseek");
  assert.equal(result.body.model, "deepseek-chat");
  assert.equal(result.body.api_key, "sk-test-1234");
  assert.equal(result.body.input_price, 0.3);
  assert.equal(result.body.daily_budget_usd, 2);
  assert.equal(result.body.tasks.length, 9);
  assert.equal(result.body.base_url, "https://api.deepseek.com/v1");
  assert.equal(result.body.max_tokens_field, "max_tokens");
});

test("免费模型可以填 0；负数、非数字和空任务被拒绝", () => {
  assert.ok(toBody({ ...filled(), inputPrice: "0", outputPrice: "0" }).ok);
  for (const bad of [{ inputPrice: "-1" }, { outputPrice: "abc" }, { dailyBudget: "" }, { tasks: [] }, { model: "" }]) {
    const result = toBody({ ...filled(), ...bad });
    assert.equal(result.ok, false, JSON.stringify(bad));
  }
});

test("需要 key 的提供方缺 key 时提示；已保存过 key 或本地网关可以留空", () => {
  const missing = toBody({ ...filled(), apiKey: "" });
  assert.equal(missing.ok, false);
  const kept = toBody({ ...filled(), apiKey: "", keySaved: true });
  assert.ok(kept.ok);
  if (kept.ok) assert.equal(kept.body.api_key, undefined);
  const local = toBody({ ...applyPreset(emptyForm(), "custom"), model: "qwen", baseUrl: "http://127.0.0.1:8000/v1", inputPrice: "0", outputPrice: "0" });
  assert.ok(local.ok);
});

test("自定义网关必须填 http(s) 地址；官方提供方不发送地址", () => {
  const noUrl = toBody({ ...applyPreset(emptyForm(), "custom"), model: "m", inputPrice: "0", outputPrice: "0" });
  assert.equal(noUrl.ok, false);
  const badUrl = toBody({ ...applyPreset(emptyForm(), "custom"), model: "m", baseUrl: "ftp://x", inputPrice: "0", outputPrice: "0" });
  assert.equal(badUrl.ok, false);
  const official = toBody({ ...applyPreset(emptyForm(), "anthropic"), model: "claude-haiku-5-5", apiKey: "k", inputPrice: "1", outputPrice: "5" });
  assert.ok(official.ok);
  if (official.ok) assert.equal(official.body.base_url, undefined);
  if (official.ok) assert.equal(official.body.max_tokens_field, undefined);
});

const settings: ModelSettingsView = {
  providers: [
    { name: "main", protocol: "fake_llm", enabled: true, base_url: null, credential_set: false, credential_last_four: null, demonstration: true },
    { name: "deepseek", protocol: "openai_chat", enabled: true, base_url: "https://api.deepseek.com/v1", credential_set: true, credential_last_four: "1234", demonstration: false },
  ],
  tasks: [
    { task: "translate", provider: "deepseek", model: "deepseek-chat", ready: true, demonstration: false, daily_budget_usd: 0.5 },
    { task: "ask", provider: "main", model: "fake", ready: true, demonstration: true, daily_budget_usd: null },
    { task: "triage", provider: null, model: null, ready: false, demonstration: false, daily_budget_usd: null },
  ],
  protocols: ["anthropic_messages", "openai_chat", "openai_responses", "gemini"],
  daily_budget_usd: 3,
};

test("已有真实配置时预填它：沿用提供方名、地址和已保存的 key，价格仍需确认", () => {
  const form = formFromSettings(settings);
  assert.equal(form.preset, "deepseek");
  assert.equal(form.provider, "deepseek");
  assert.equal(form.model, "deepseek-chat");
  assert.equal(form.keySaved, true);
  assert.equal(form.keyHint, "1234");
  assert.equal(form.dailyBudget, "3");
});

test("修改已有配置只预选这个提供方的任务和还没有真实模型的任务，不抢走其他提供方的任务", () => {
  const view: ModelSettingsView = { ...settings, tasks: [...settings.tasks,
    { task: "extract", provider: "other", model: "x", ready: true, demonstration: false, daily_budget_usd: null }] };
  const tasks = formFromSettings(view).tasks;
  assert.ok(!tasks.includes("extract"));
  for (const task of ["translate", "ask", "triage"] as const) assert.ok(tasks.includes(task));
});

test("客户端校验与服务端一致：上限、地址里的账号密码", () => {
  assert.equal(toBody({ ...filled(), inputPrice: "2000000" }).ok, false);
  assert.equal(toBody({ ...filled(), maxOutputTokens: "300000" }).ok, false);
  assert.equal(toBody({ ...filled(), baseUrl: "https://user:pw@api.example.com/v1" }).ok, false);
  assert.equal(toBody({ ...filled(), baseUrl: "https://api.example.com/v1?x=1" }).ok, false);
  assert.equal(toBody({ ...filled(), inputPrice: "0x10" }).ok, false);
});

test("任务状态摘要区分已配置、演示模式和未配置", () => {
  const rows = taskSummary(settings);
  assert.deepEqual(rows.map((row) => row.status), ["ready", "demo", "missing"]);
  assert.match(rows[0].label, /翻译/);
});

test("测试结果的文字说明能力、花费和失败原因", () => {
  assert.match(probeText({ ok: true, capabilities: ["structured", "stream"], cost_usd: 0.0001, estimated_usd: 0.0002 }), /连接成功.*结构化.*流式/);
  assert.match(probeText({ ok: false, capabilities: [], cost_usd: 0, estimated_usd: 0, code: "model_connection_failed" }), /连接失败/);
});

test("测试费用上限按两次调用、输出不超过 1024 token 计算；价格没填时不显示", () => {
  assert.equal(probeCeiling({ ...filled(), inputPrice: "1", outputPrice: "10" }), 2 * (128 + 10240) / 1e6);
  assert.equal(probeCeiling({ ...filled(), inputPrice: "1", outputPrice: "10", maxOutputTokens: "300" }), 2 * (128 + 3000) / 1e6);
  assert.equal(probeCeiling({ ...filled(), inputPrice: "" }), null);
});
