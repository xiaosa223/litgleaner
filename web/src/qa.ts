// 后端已核验的问答；作者、用户和模型来源分别保留。
export interface QaSelection { text: string; page: number | null }
export interface QaSubmission {
  text: string; selection: string | null; page: number | null; request_id: string;
}
export interface QaCitation { paper_id: string; page: number; quote: string }
export interface QaNoteCitation {
  paper_id: string; source_id: string; quote: string; kind: "note" | "question";
}
export interface QaStatement {
  text: string; inference: boolean; citations: QaCitation[];
  note_citations: QaNoteCitation[]; mentioned_paper_ids: string[];
}
export interface QaView {
  question: {
    id: string; paper_id: string; text: string; selection: string | null;
    page: number | null; request_id: string; created_at: string;
  };
  answer: { id: string; statements: QaStatement[]; model: string; context?: { papers?: Record<string, string> } } | null;
  state: "queued" | "retrying" | "done" | "failed" | "configuration_required" | "budget_exceeded";
  code: string | null;
}

export const qaWaiting = (view: QaView): boolean => !view.answer && view.state !== "failed" && view.state !== "done";

export function selectForQa(text: string | null, page: number | null): QaSelection | null {
  if (text && text.length > 6000) throw new Error("选区过长，请缩小到 6000 字符以内后再提问");
  return text ? { text, page } : null;
}

export function qaStatementText(text: string, papers: Record<string, string> = {}): string {
  return text.replace(/\[\[paper:([^\]]+)\]\]/g, (_marker, id: string) => `《${papers[id] || id}》`);
}

export function mergeQa(current: QaView[], incoming: QaView[]): QaView[] {
  const rows = new Map(current.map((row) => [row.question.id, row]));
  for (const row of incoming) {
    const previous = rows.get(row.question.id);
    // 在途的旧历史响应不能把已经收到的答案退回排队状态。
    if (!previous?.answer || row.answer) rows.set(row.question.id, row);
  }
  return [...rows.values()].sort((a, b) =>
    a.question.created_at.localeCompare(b.question.created_at) || a.question.id.localeCompare(b.question.id));
}

export function submission(text: string, selected: QaSelection | null, previous: QaSubmission | null): QaSubmission {
  const fields = { text: text.trim(), selection: selected?.text ?? null, page: selected?.page ?? null };
  if (previous && previous.text === fields.text && previous.selection === fields.selection && previous.page === fields.page) return previous;
  return { ...fields, request_id: crypto.randomUUID() };
}

export function qaStatus(view: QaView): string {
  switch (view.state) {
    case "queued": return "问题已保存，正在等待回答…";
    case "retrying": return "暂时未能完成，服务正在重试…";
    case "configuration_required": return "尚未配置问答模型。配置 ask 任务后，此问题会继续处理。";
    case "budget_exceeded": return "模型预算已用完，问题已保留，额度恢复后继续处理。";
    case "failed": return "这次回答未能完成。可以重新提问；新的提问可能产生模型费用。";
    case "done": return "没有可显示的回答，请刷新状态。";
  }
}
