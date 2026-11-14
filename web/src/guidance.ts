// 个性化导读、回答卡片、掌握程度与复习的数据结构和纯逻辑（第 12.7 节）。
import type { Mastery } from "./types";

export type QuestionKind = "understand" | "compare" | "transfer";
export type Treatment = "full" | "remind" | "brief";
export type JobState = "none" | "queued" | "retrying" | "failed" | "configuration_required" | "budget_exceeded";

export interface Quote { page: number; quote: string }
export interface Sourced { text: string; citations: Quote[]; inference: boolean }

export interface GuideConcept {
  concept_id: string; name: string; level: Mastery; treatment: Treatment;
  explanation: Sourced;
  analogy: { concept_id: string | null; name: string; text: string; breaks: string } | null;
  anchor: string | null;
}
export interface GuideQuestion {
  index: number; kind: QuestionKind; text: string; concept_ids: string[];
  section: string | null; goal_id: string | null;
}
export interface Guide {
  id: string; paper_id: string; created_at: string; model: string; knowledge_empty: boolean;
  overview: Sourced; concepts: GuideConcept[]; questions: GuideQuestion[];
  sections: { section: string; why: string }[];
}
export interface AnswerCard { id: string; question_index: number; revision: number; text: string; created_at: string }
export interface Evaluation {
  id: string; card_id: string; overall: "correct" | "partial" | "incorrect";
  verdicts: { concept_id: string; verdict: "correct" | "missing" | "misunderstood"; comment: string; citations: Quote[] }[];
  reference: Sourced; advice: string;
}
export interface CardView {
  question_index: number; card: AnswerCard | null; revisions: number;
  evaluation: Evaluation | null; evaluation_state: JobState;
}
export interface GuidePage {
  view: {
    paper_id: string; guide: Guide | null; stale: boolean; job: JobState; unavailable: string | null;
    plan: {
      configured: boolean; plan_hash: string | null; estimated_usd: number | null; knowledge_empty: boolean;
      concepts: { concept_id: string; name: string; level: Mastery }[];
    } | null;
  };
  cards: CardView[];
  evaluate_configured: boolean;
  evaluate_estimated_usd: number | null;
}

export interface MasteryState {
  level: Mastery; overridden: boolean; due_at: string | null; step: number; remembered: number; reviews: number;
}
export interface UserAnswer {
  card_id: string; paper_id: string; paper_title: string; question: string; text: string; created_at: string;
  verdict: "correct" | "missing" | "misunderstood" | null;
  overall: "correct" | "partial" | "incorrect" | null;
}

/** 回答旁的评价提示：理解偏了的要明确提醒，避免复习时把错误理解当参照。 */
export function answerNote(verdict: UserAnswer["verdict"], overall: UserAnswer["overall"] = null): string | null {
  if (verdict === "misunderstood") return "当时评价：理解偏了，别以这句为准";
  if (verdict === "missing") return "当时评价：没说错，但漏了要点";
  if (overall === "partial" || overall === "incorrect") return "这个概念说对了，但这段回答里还有别处不准确";
  return null;
}
export interface ConceptSummary {
  concept: { id: string; name: string };
  aliases: { name: string; language: string | null }[];
  papers: number;
  definition: { quote: string; paper_id: string; page: number; stale: boolean } | null;
}
export interface RelatedView {
  link_id: string; kind: "prerequisite" | "similar" | "contrast"; direction: "out" | "in";
  concept_id: string; name: string; reason: string; confirmed: boolean;
}
export interface MergeView { merge_id: string; from_id: string; from_name: string; into_id: string; into_name: string; reason: string }
export interface ConceptCard {
  summary: ConceptSummary; mastery: MasteryState; due: boolean; answers: UserAnswer[];
  related: RelatedView[]; merged: { merge_id: string; concept_id: string; name: string }[];
}

/** 关系的读法：前置是有方向的，相似、对比不分方向。 */
export function relationLabel(item: RelatedView): string {
  if (item.kind === "prerequisite") return item.direction === "out" ? "先懂" : "是它的前置";
  return item.kind === "similar" ? "相似" : "对比";
}
export interface ConceptCards { items: ConceptCard[]; total: number; due: number }
export type Rating = "remembered" | "fuzzy" | "forgot";

export const KIND_LABEL: Record<QuestionKind, string> = { understand: "理解", compare: "对比", transfer: "迁移" };
export const KIND_HINT: Record<QuestionKind, string> = {
  understand: "用自己的话说出核心机制",
  compare: "和你已经懂的东西比一比",
  transfer: "想想能不能用到你的课题上",
};
export const TREATMENT_LABEL: Record<Treatment, string> = { full: "新概念", remind: "见过", brief: "你已掌握" };
export const VERDICT_LABEL = { correct: "对", missing: "漏了", misunderstood: "理解偏了" } as const;
export const OVERALL_LABEL = { correct: "答得对", partial: "部分正确", incorrect: "需要再想想" } as const;
export const RATING_LABEL: Record<Rating, string> = { remembered: "记得", fuzzy: "模糊", forgot: "忘了" };
export const RATINGS: Rating[] = ["remembered", "fuzzy", "forgot"];

/** 后台还在跑、需要继续轮询的状态。 */
export const waiting = (state: JobState) => state === "queued" || state === "retrying";

export function guideWaiting(page: GuidePage | null): boolean {
  if (!page) return false;
  return waiting(page.view.job) || page.cards.some((card) => waiting(card.evaluation_state));
}

/** 费用提示：有历史平均费用时给出数字，否则只说明会调用模型。 */
export function costHint(usd: number | null): string {
  if (usd === null) return "会调用一次模型";
  if (usd < 0.001) return "费用不到 $0.001";
  return `约 $${usd < 0.01 ? usd.toFixed(3) : usd.toFixed(2)}`;
}

/** 概念的译名：优先阅读语言（中文）的别名。 */
export function translationOf(summary: ConceptSummary): string {
  const zh = summary.aliases.find((alias) => alias.language?.startsWith("zh") && alias.name !== summary.concept.name);
  return zh?.name ?? "";
}

/** 字数提示：一到三句话，太长时提醒精简。 */
export function lengthHint(text: string): string | null {
  const trimmed = text.trim();
  if (!trimmed) return null;
  const sentences = trimmed.split(/[。！？.!?]+\s*/).filter(Boolean).length;
  if (sentences > 4 || trimmed.length > 400) return "写得有点长了，试着压缩到一到三句话，更容易看出自己哪里懂了。";
  return null;
}

export function dueLabel(dueAt: string | null, now: Date = new Date()): string {
  if (!dueAt) return "";
  const days = Math.ceil((new Date(dueAt).getTime() - now.getTime()) / 86_400_000);
  if (days <= 0) return "今天复习";
  return days === 1 ? "明天复习" : `${days} 天后复习`;
}

/** 一次评价让这个概念达到的等级（与后端 domain/mastery.py 的 evaluation_level 同规则）。 */
export function provenLevel(kind: QuestionKind, verdict: Evaluation["verdicts"][number]["verdict"], overall: Evaluation["overall"]): Mastery {
  if (verdict !== "correct") return 1;
  return kind !== "understand" && overall === "correct" ? 3 : 2;
}

export type MetadataField = "title" | "authors" | "venue" | "published_at";
export interface MetadataView {
  paper_id: string; pending: boolean;
  corrected: Partial<Record<MetadataField, string>>;
  title_suggestion: string | null;
}
