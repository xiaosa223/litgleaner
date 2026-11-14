// 课题设置：研究描述的编辑表、启用与暂停、按新描述重新筛选已有论文（第 7.1、7.3 节）。
// 纯函数，可单测。接口说明见 docs/TOPIC-DRAFT.md。
import { money, type TopicSummary } from "./topicView.ts";

/** GET /api/topics/{id} */
export interface TopicDetail {
  id: string; name: string; background: string; content: string;
  goals: { id: string; text: string }[]; status: TopicSummary["status"]; terms: number;
}

export interface ProfileForm { name: string; background: string; content: string; goals: { id: string | null; text: string }[] }

export interface ProfileBody { name: string; background: string; content: string; goals: { id: string | null; text: string }[] }

export interface BackfillPlan { papers: number; estimated_usd: number | null; plan: string; held?: number }

/** 与服务端的校验一致（server/topic_settings.py） */
export const LIMITS = { name: 100, text: 8000, goal: 1000, goals: 20 } as const;

export const formFrom = (topic: TopicDetail): ProfileForm => ({
  name: topic.name, background: topic.background, content: topic.content,
  goals: topic.goals.map((goal) => ({ id: goal.id, text: goal.text })),
});

const clean = (form: ProfileForm): ProfileBody => ({
  name: form.name.trim(), background: form.background.trim(), content: form.content.trim(),
  goals: form.goals.map((goal) => ({ id: goal.id, text: goal.text.trim() })).filter((goal) => goal.text),
});

export const isDirty = (form: ProfileForm, topic: TopicDetail) =>
  JSON.stringify(clean(form)) !== JSON.stringify(clean(formFrom(topic)));

export function profileBody(form: ProfileForm): { body: ProfileBody | null; error: string | null } {
  const body = clean(form);
  const missing = [["课题名称", body.name], ["研究背景", body.background], ["研究内容", body.content]].find(([, value]) => !value);
  if (missing) return { body: null, error: `${missing[0]}不能为空。` };
  if (body.goals.length === 0) return { body: null, error: "至少要有一个研究目标。" };
  if (body.name.length > LIMITS.name) return { body: null, error: `课题名称最多 ${LIMITS.name} 字。` };
  if (body.goals.length > LIMITS.goals) return { body: null, error: `研究目标最多 ${LIMITS.goals} 个。` };
  if (body.goals.some((goal) => goal.text.length > LIMITS.goal)) return { body: null, error: `每个研究目标最多 ${LIMITS.goal} 字。` };
  if ([body.background, body.content].some((text) => text.length > LIMITS.text)) return { body: null, error: `研究背景和研究内容各最多 ${LIMITS.text} 字。` };
  return { body, error: null };
}

const ERRORS: Record<string, string> = {
  topic_changed_on_disk: "课题文件刚被别处改过。刷新页面看到最新内容后再改。",
  topic_query_missing: "这个课题还没有检索词，启用了也搜不到论文。先在“检索词”里起草并保存。",
  topic_goals_invalid: "研究目标的编号对不上，刷新页面后再改。",
  topic_incomplete: "名称、背景、内容和至少一个目标都要填。",
  topic_too_large: "名称最多 100 字，研究目标最多 20 个。",
  topic_invalid: "课题内容没有通过检查，刷新页面后再改。",
  topic_settings_unavailable: "当前运行方式不支持在界面里改课题，请用命令行或直接编辑课题文件。",
  topic_save_failed: "没有保存成功：数据目录可能不可写，检查后再试。",
  topic_not_active: "课题启用后才能重新筛选已有论文。",
  plan_changed: "文库或课题描述刚有变化，重新看一下篇数和费用再确认。",
};

export const settingsError = (message: string) => ERRORS[message] ?? message;

export function backfillText(plan: BackfillPlan): string {
  const held = plan.held ? `另有 ${plan.held} 篇正由课题调研处理，调研完成或在“调研进度”里重试后会自动筛选，这里不重复排队。` : "";
  if (plan.papers === 0) return held || "文库里的论文都已按当前描述筛选过，不需要重新筛选。";
  const cost = plan.estimated_usd === null ? "还没有初筛的历史费用，暂时估不出金额" : `初筛预计约 ${money(plan.estimated_usd)}`;
  return `文库里有 ${plan.papers} 篇论文还没按当前描述筛选过。${cost}；通过初筛的论文之后会照常取全文和评估，这部分另算费用。${held}`;
}
