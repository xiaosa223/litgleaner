import { paperPageLink } from "./pageLink.ts";
import type { SearchHit } from "./types.ts";

export interface PaletteHit { key: string; group: string; title: string; meta: string; go: string }

const GROUPS: Record<SearchHit["kind"], string> = {
  paper: "论文", fulltext: "全文", note: "笔记", highlight: "高亮", extraction: "提取摘要", qa: "问答",
};
const AUTHORS: Record<SearchHit["authorship"], string> = { author: "作者原文", user: "我的记录", model: "模型生成" };
const ORDER: SearchHit["kind"][] = ["paper", "fulltext", "note", "highlight", "extraction", "qa"];

/** 同类结果保持服务端相关性次序；不同类型集中显示，保留出处和原文页码。 */
export function paletteHits(hits: SearchHit[]): PaletteHit[] {
  return ORDER.flatMap((kind) => hits.filter((hit) => hit.kind === kind).map((hit) => ({
    key: `${hit.paper_id}:${hit.kind}:${hit.id}`,
    group: GROUPS[kind],
    title: hit.title,
    meta: [AUTHORS[hit.authorship], hit.page === null ? null : `第 ${hit.page + 1} 页`, hit.text].filter(Boolean).join(" · "),
    go: paperPageLink(hit.paper_id, hit.page),
  })));
}
