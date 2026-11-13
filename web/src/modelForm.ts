// 模型原地配置的纯逻辑：预设、表单校验、请求体、状态摘要（第 15.4、15.5 节，docs/MODEL-SETUP.md）。
// 预设只填协议和地址；价格变化快，不写死，由用户按官网价格页填写。

export type ModelTask = "triage" | "extract" | "assess" | "translate" | "ask" | "map" | "outline" | "relate" | "draft";

export const TASKS: { task: ModelTask; label: string; hint: string }[] = [
  { task: "translate", label: "翻译", hint: "阅读时按段落翻译" },
  { task: "ask", label: "边读边问", hint: "针对论文提问，回答附原文出处" },
  { task: "draft", label: "起草检索词", hint: "新建课题时根据研究画像起草检索词和术语表" },
  { task: "triage", label: "每日初筛", hint: "从新论文里挑出和课题相关的" },
  { task: "extract", label: "提炼要点", hint: "整理问题、做法、结论和不足" },
  { task: "assess", label: "评估价值", hint: "按评价标准给出推荐理由" },
  { task: "map", label: "课题地图", hint: "把调研到的论文分组" },
  { task: "outline", label: "综述骨架", hint: "根据已读论文起草综述提纲" },
  { task: "relate", label: "论文关系核实", hint: "核对两篇论文之间的关系" },
];
const ALL_TASKS = TASKS.map((item) => item.task);
export const taskLabel = (task: string) => TASKS.find((item) => item.task === task)?.label ?? task;

export type PresetId = "deepseek" | "zhipu" | "openai" | "anthropic" | "gemini" | "custom";

export type MaxTokensField = "max_tokens" | "max_completion_tokens" | ""; // 空串：沿用已有配置或协议默认

export interface Preset {
  id: PresetId;
  name: string;
  note: string;
  protocol: string;
  baseUrl: string; // 空串表示用官方默认地址
  structured: "native" | "prompt";
  maxTokensField: MaxTokensField; // 兼容接口常只认 max_tokens
  model: string; // 有把握的默认模型名；没有把握时留空
  modelHint: string;
  keyUrl: string | null;
  priceUrl: string | null;
  needsKey: boolean;
}

export const PRESETS: Preset[] = [
  {
    id: "deepseek", name: "DeepSeek", note: "国内可直连，价格低", protocol: "openai_chat",
    baseUrl: "https://api.deepseek.com/v1", structured: "prompt", maxTokensField: "max_tokens", model: "deepseek-chat", modelHint: "deepseek-chat 是通用对话模型",
    keyUrl: "https://platform.deepseek.com/", priceUrl: "https://api-docs.deepseek.com/quick_start/pricing", needsKey: true,
  },
  {
    id: "zhipu", name: "智谱 GLM", note: "国内可直连，有免费模型", protocol: "openai_chat",
    baseUrl: "https://open.bigmodel.cn/api/paas/v4", structured: "prompt", maxTokensField: "max_tokens", model: "", modelHint: "免费模型名以官网模型列表为准",
    keyUrl: "https://open.bigmodel.cn/", priceUrl: "https://open.bigmodel.cn/", needsKey: true,
  },
  {
    id: "openai", name: "OpenAI", note: "需要能访问 OpenAI", protocol: "openai_responses",
    baseUrl: "", structured: "native", maxTokensField: "", model: "", modelHint: "模型名见官网",
    keyUrl: "https://platform.openai.com/api-keys", priceUrl: "https://openai.com/api/pricing/", needsKey: true,
  },
  {
    id: "anthropic", name: "Anthropic Claude", note: "需要能访问 Anthropic", protocol: "anthropic_messages",
    baseUrl: "", structured: "native", maxTokensField: "", model: "", modelHint: "例如 claude-haiku-5-5",
    keyUrl: "https://console.anthropic.com/", priceUrl: "https://www.anthropic.com/pricing", needsKey: true,
  },
  {
    id: "gemini", name: "Google Gemini", note: "需要能访问 Google", protocol: "gemini",
    baseUrl: "", structured: "native", maxTokensField: "", model: "", modelHint: "模型名见官网",
    keyUrl: "https://aistudio.google.com/apikey", priceUrl: "https://ai.google.dev/pricing", needsKey: true,
  },
  {
    id: "custom", name: "其他 / 本地网关", note: "OpenAI 兼容接口：中转站、学校或本机部署的模型", protocol: "openai_chat",
    baseUrl: "", structured: "prompt", maxTokensField: "max_tokens", model: "", modelHint: "填服务提供的模型名",
    keyUrl: null, priceUrl: null, needsKey: false,
  },
];
export const preset = (id: PresetId): Preset => PRESETS.find((item) => item.id === id) ?? PRESETS[PRESETS.length - 1];

export interface ModelForm {
  preset: PresetId;
  provider: string;
  protocol: string;
  model: string;
  baseUrl: string;
  apiKey: string;
  keySaved: boolean; // 这个提供方已经存过 key，留空表示沿用
  keyHint: string | null; // 已存 key 的末四位
  inputPrice: string;
  outputPrice: string;
  dailyBudget: string;
  tasks: ModelTask[];
  structured: "native" | "prompt";
  maxTokensField: MaxTokensField;
  maxOutputTokens: string;
}

export const emptyForm = (): ModelForm => applyPreset({
  preset: "deepseek", provider: "", protocol: "", model: "", baseUrl: "", apiKey: "", keySaved: false, keyHint: null,
  inputPrice: "", outputPrice: "", dailyBudget: "2", tasks: [...ALL_TASKS], structured: "prompt", maxTokensField: "", maxOutputTokens: "4000",
}, "deepseek");

/** 换预设：改协议、地址、结构化方式和提供方名；模型名、价格、key 清空，避免把上一家的值带过去。 */
export function applyPreset(form: ModelForm, id: PresetId): ModelForm {
  const item = preset(id);
  return {
    ...form, preset: id, provider: id, protocol: item.protocol, baseUrl: item.baseUrl, structured: item.structured, maxTokensField: item.maxTokensField,
    model: item.model, apiKey: "", keySaved: false, keyHint: null, inputPrice: "", outputPrice: "",
  };
}

export interface ModelProviderView {
  name: string; protocol: string; enabled: boolean; base_url: string | null;
  credential_set: boolean; credential_last_four: string | null; demonstration: boolean;
}
export interface ModelTaskView {
  task: string; provider: string | null; model: string | null; ready: boolean;
  demonstration: boolean; daily_budget_usd: number | null;
}
export interface ModelSettingsView {
  providers: ModelProviderView[]; tasks: ModelTaskView[]; protocols: string[]; daily_budget_usd: number;
}
export interface ProbeResult { ok: boolean; capabilities: string[]; cost_usd: number; estimated_usd: number; code?: string | null }

export interface ModelBody {
  provider: string; protocol: string; model: string; tasks: ModelTask[];
  input_price: number; output_price: number; api_key?: string; base_url?: string;
  daily_budget_usd: number; max_output_tokens: number; structured: "native" | "prompt";
  max_tokens_field?: "max_tokens" | "max_completion_tokens";
}

const presetFor = (provider: ModelProviderView): PresetId => {
  const known = PRESETS.find((item) => item.id === provider.name && item.protocol === provider.protocol);
  if (known) return known.id;
  const byUrl = PRESETS.find((item) => item.baseUrl && item.baseUrl === provider.base_url);
  return byUrl?.id ?? "custom";
};

/** 已有真实（非演示）配置时以它为起点；价格接口不返回，需要重新确认。 */
export function formFromSettings(view: ModelSettingsView): ModelForm {
  const base = { ...emptyForm(), dailyBudget: String(view.daily_budget_usd) };
  const task = view.tasks.find((item) => item.ready && !item.demonstration && item.provider);
  const provider = task && view.providers.find((item) => item.name === task.provider);
  if (!task || !provider) return base;
  const id = presetFor(provider);
  // 只预选这个提供方的任务和还没有真实模型的任务；其他提供方的任务保持原样
  const tasks = ALL_TASKS.filter((name) => {
    const row = view.tasks.find((item) => item.task === name);
    return !row || row.provider === provider.name || !row.ready || row.demonstration;
  });
  return {
    ...applyPreset(base, id),
    tasks,
    provider: provider.name,
    protocol: provider.protocol,
    baseUrl: provider.base_url ?? "",
    model: task.model ?? "",
    maxTokensField: "", // 状态接口不返回；留空表示保存时沿用已有设置
    keySaved: provider.credential_set,
    keyHint: provider.credential_last_four,
  };
}

// 与服务端 ModelBody 的上限一致；只接受普通小数，拒绝 0x10、1e3 这类写法
const LIMIT = 1_000_000;
const number = (text: string, max = LIMIT): number | null => {
  const trimmed = text.trim();
  if (!/^\d+(\.\d+)?$/.test(trimmed)) return null;
  const value = Number(trimmed);
  return value <= max ? value : null;
};

/** 与服务端一致：http(s)、有主机名，不带账号密码、查询参数和片段。 */
function validUrl(text: string): boolean {
  try {
    const url = new URL(text);
    return ["http:", "https:"].includes(url.protocol) && !!url.hostname &&
      !url.username && !url.password && !url.search && !url.hash && !text.includes("#") && !text.includes("?");
  } catch {
    return false;
  }
}

export type BodyResult = { ok: true; body: ModelBody } | { ok: false; error: string };

export function toBody(form: ModelForm): BodyResult {
  const item = preset(form.preset);
  const model = form.model.trim();
  const key = form.apiKey.trim();
  const url = form.baseUrl.trim();
  const inputPrice = number(form.inputPrice);
  const outputPrice = number(form.outputPrice);
  const budget = number(form.dailyBudget);
  const tokens = number(form.maxOutputTokens, 200_000);
  if (!model) return { ok: false, error: "请填写模型名" };
  if (item.needsKey && !key && !form.keySaved) return { ok: false, error: "请填写 API key" };
  if (url && !validUrl(url)) return { ok: false, error: "接口地址要以 http:// 或 https:// 开头，不能带账号密码或 ? # 后缀" };
  if (!url && form.preset === "custom") return { ok: false, error: "请填写接口地址，例如 http://127.0.0.1:8000/v1" };
  if (inputPrice === null || outputPrice === null) return { ok: false, error: "请按官网填写输入和输出价格（美元/百万 token，不超过 1000000），免费模型填 0" };
  if (budget === null) return { ok: false, error: "每日上限要填一个不小于 0 的数" };
  if (tokens === null || tokens < 128 || !Number.isInteger(tokens)) return { ok: false, error: "单次输出上限要填 128 到 200000 之间的整数" };
  if (form.tasks.length === 0) return { ok: false, error: "至少选一项用到模型的功能" };
  const body: ModelBody = {
    provider: form.provider, protocol: form.protocol, model, tasks: form.tasks,
    input_price: inputPrice, output_price: outputPrice, daily_budget_usd: budget,
    max_output_tokens: tokens, structured: form.structured,
  };
  if (key) body.api_key = key;
  if (url) body.base_url = url;
  if (form.maxTokensField && form.protocol === "openai_chat") body.max_tokens_field = form.maxTokensField;
  return { ok: true, body };
}

export type TaskStatus = "ready" | "demo" | "missing";
export function taskSummary(view: ModelSettingsView): { task: string; label: string; status: TaskStatus; model: string | null; budget: number | null }[] {
  return view.tasks.map((item) => ({
    task: item.task,
    label: taskLabel(item.task),
    status: !item.ready ? "missing" : item.demonstration ? "demo" : "ready",
    model: item.model,
    budget: item.daily_budget_usd,
  }));
}

/** 连接测试的费用上限：最多两次调用，每次输入约 128、输出不超过 1024 token（与服务端一致）。 */
export function probeCeiling(form: ModelForm): number | null {
  const input = number(form.inputPrice);
  const output = number(form.outputPrice);
  const limit = number(form.maxOutputTokens);
  if (input === null || output === null || limit === null) return null;
  return (2 * (128 * input + Math.min(limit, 1024) * output)) / 1_000_000;
}

const usd = (value: number) => `$${value < 0.01 ? value.toFixed(4) : value.toFixed(2)}`;

export function probeText(result: ProbeResult): string {
  const abilities = result.capabilities.map((name) => (name === "structured" ? "结构化输出" : name === "stream" ? "流式输出" : name));
  if (result.ok) return `连接成功${abilities.length ? `，支持${abilities.join("、")}` : ""}。本次测试花费 ${usd(result.cost_usd)}。`;
  return "连接失败：请检查 API key、模型名和接口地址是否正确，账户是否有余额。" +
    (result.cost_usd > 0 ? `本次测试花费 ${usd(result.cost_usd)}。` : "");
}
