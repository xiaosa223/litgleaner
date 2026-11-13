// 检索词命中篇数（第 7.2 节）：近一年在来源里能搜到多少篇，帮助判断太宽或太窄。纯函数，可单测。
import { splitList, type Editor } from "./topicQuery.ts";

export interface TermHits { term: string; hits: number | null; error: string | null }
export interface TermCounts { source: string; since: string; until: string; counts: TermHits[] }
export interface CountBody { include: { term: string; synonyms: string[] }[]; exclude: string[] }

type Row = Editor["include"][number];

const norm = (text: string) => text.trim().replace(/\s+/g, " ").toLowerCase();

/** 一行检索词连同同义词的指纹；改了任何一个，旧篇数就不再对应 */
export const rowKey = (row: Row) => [norm(row.term), ...splitList(row.synonyms).map(norm)].join("|");

export function countBody(editor: Editor): CountBody | null {
  const include = editor.include
    .filter((row) => row.term.trim())
    .map((row) => ({ term: row.term.trim(), synonyms: splitList(row.synonyms) }));
  if (include.length === 0) return null;
  return { include, exclude: editor.exclude.map((t) => t.trim()).filter(Boolean) };
}

const MAX_TERM = 120; // 与服务端一致（server/topic_drafts.py）

/** 有词超过服务端的长度上限：本地拦下，给出能照着改的话 */
export const tooLong = (editor: Editor) =>
  [...editor.include.flatMap((row) => [row.term, ...splitList(row.synonyms)]), ...editor.exclude].some((t) => t.trim().length > MAX_TERM);

export const countFor = (counted: Map<string, TermHits>, row: Row) => counted.get(rowKey(row)) ?? null;

// 粗略的经验线：日常跟踪会把这些论文送去初筛，太宽意味着更多费用和噪声
const NARROW = 50;
const BROAD = 20_000;

export function hitsText(item: TermHits): string {
  if (item.hits === null) return "这个词暂时没查到篇数，稍后再试。";
  if (item.hits === 0) return "近一年没有搜到（已去掉含排除词的论文）。检查拼写和排除词，或换成论文里更常见的说法。";
  const count = item.hits.toLocaleString("en-US");
  const daily = Math.round(item.hits / 365);
  if (item.hits < NARROW) return `近一年 ${count} 篇，偏窄，可能漏掉相关论文；可以补充同义词或更通用的说法。`;
  const perDay = daily >= 1 ? `，每天约 ${daily.toLocaleString("en-US")} 篇` : "";
  if (item.hits > BROAD) return `近一年约 ${count} 篇${perDay}，很宽：日常跟踪会送来大量论文去初筛，费用和无关论文都会多，考虑换成更具体的说法。`;
  return `近一年约 ${count} 篇${perDay}。`;
}
