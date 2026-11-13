// 检索词命中篇数：按编辑表里还没保存的词，向来源查近一年的篇数（第 7.2 节）。不调用模型，每行一次检索请求。
import { useState } from "react";
import { topicApi } from "../api";
import { countBody, countFor, hitsText, rowKey, tooLong, type TermHits } from "../termCounts";
import type { Editor } from "../topicQuery";

const COUNT_ERRORS: Record<string, string> = {
  term_count_unavailable: "没有启用能查篇数的来源（目前只有 OpenAlex）。在 config/sources.yaml 里启用 OpenAlex 后再查。",
  query_invalid: "检索词有问题：检查是否有空行或过长的词。",
};

const excludeKey = (editor: Editor) => editor.exclude.map((t) => t.trim().toLowerCase()).filter(Boolean).sort().join("|");

export function useTermCounts(topicId: string) {
  const [counted, setCounted] = useState<{ rows: Map<string, TermHits>; exclude: string; source: string } | null>(null);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const run = async (editor: Editor) => {
    const body = countBody(editor);
    if (!body) { setError("先填至少一个检索词。"); return; }
    if (tooLong(editor)) { setError("有的词超过 120 个字符，缩短后再查。"); return; }
    setBusy(true); setError(null);
    try {
      const result = await topicApi.termCounts(topicId, body);
      const rows = editor.include.filter((row) => row.term.trim());
      setCounted({ rows: new Map(rows.map((row, i) => [rowKey(row), result.counts[i]])), exclude: excludeKey(editor), source: result.source });
    } catch (cause) {
      setError(cause instanceof Error ? COUNT_ERRORS[cause.message] ?? cause.message : "查询失败");
    } finally { setBusy(false); }
  };
  /** 改了这一行或排除词之后，旧篇数不再对应，返回 null */
  const hitsFor = (editor: Editor, row: Editor["include"][number]) =>
    counted && counted.exclude === excludeKey(editor) ? countFor(counted.rows, row) : null;
  return { run, busy, error, hitsFor, source: counted?.source ?? null };
}

export type TermCountState = ReturnType<typeof useTermCounts>;

export const Hits = ({ item }: { item: TermHits | null }) =>
  item ? <div className="why"><span className="badge">篇数</span> {hitsText(item)}</div> : null;

export function CountButton({ state, editor }: { state: TermCountState; editor: Editor }) {
  return (
    <div className="query-count">
      <button className="ghost sm" disabled={state.busy} onClick={() => void state.run(editor)}>{state.busy ? "正在查篇数…" : "查每个词能搜到多少篇"}</button>
      <span className="tag">按当前排除词统计近一年的篇数，不同检索词搜到的论文会有重叠。不花模型费用；每个词向{state.source ? ` ${state.source} ` : "论文数据库"}发一次检索，通常不到一分钟。</span>
      {state.error && <div className="hint-line bad" role="alert">{state.error}</div>}
    </div>
  );
}
