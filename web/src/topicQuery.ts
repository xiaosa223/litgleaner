// 检索词和术语表的编辑（第 7.2 节，docs/TOPIC-DRAFT.md）：草稿与当前检索式转成编辑表，编辑表转成请求体。纯函数，可单测。

export interface DraftTerm { term: string; reason: string; synonyms: string[] }
export interface GlossaryItem { forms: string[]; translation: string; definition: string | null; source: string | null }

export interface QueryDraft {
  topic_id: string; plan_hash: string; model: string; created_at: string; reading_language: string;
  include: DraftTerm[]; exclude: { term: string; reason: string }[]; glossary: GlossaryItem[];
  seed_titles: string[]; warnings: string[];
}

export interface TopicQuery { include: string[]; synonyms: Record<string, string[]>; exclude: string[] }

export interface DraftView { topic_id: string; draft: QueryDraft | null; stale: boolean; current: TopicQuery; glossary: GlossaryItem[] }
export interface DraftPreview {
  topic_id: string; plan_hash: string; estimated_usd: number | null; cached: boolean;
  seed_titles: string[]; seeds_missing: string[]; current: TopicQuery;
}
export interface DraftStatus { job_key: string; status: "pending" | "done" | "dead"; draft: QueryDraft | null; cost_usd: number; last_error: string | null }

export interface Editor {
  include: { term: string; synonyms: string }[];
  exclude: string[];
  glossary: { term: string; translation: string; definition: string }[]; // term：原文写法，多个用逗号分隔
}

export interface QueryBody {
  include: { term: string; synonyms: string[] }[];
  exclude: string[];
  glossary: { forms: string[]; translation: string; definition: string | null }[];
}

const glossaryRows = (items: GlossaryItem[]) =>
  items.map((g) => ({ term: g.forms.join(", "), translation: g.translation, definition: g.definition ?? "" }));

export function editorFrom(query: TopicQuery, glossary: GlossaryItem[]): Editor {
  return {
    include: query.include.map((term) => ({ term, synonyms: (query.synonyms[term] ?? []).join(", ") })),
    exclude: [...query.exclude],
    glossary: glossaryRows(glossary),
  };
}

export function draftEditor(draft: QueryDraft): Editor {
  return {
    include: draft.include.map((t) => ({ term: t.term, synonyms: t.synonyms.join(", ") })),
    exclude: draft.exclude.map((t) => t.term),
    glossary: glossaryRows(draft.glossary),
  };
}

const key = (text: string) => text.trim().replace(/\s+/g, " ").toLowerCase();

const ERRORS: Record<string, string> = {
  topic_save_failed: "没有保存成功：课题文件写不进去，可能是数据目录不可写或磁盘满了。",
  topic_changed_on_disk: "课题文件在别处被改过。刷新页面看到最新内容后再改。",
  query_invalid: "检索词有问题：至少要有一个，排除词不能和检索词重复。",
  glossary_invalid: "术语表有一行写法为空，请补上或删掉。",
  topic_settings_unavailable: "当前运行方式不支持在页面里修改课题，请改课题文件。",
};

/** 服务端的错误码换成能照着做的话；不认识的原样显示。 */
export const queryError = (message: string) => ERRORS[message] ?? message;

/** 草稿里这个词的选择理由；不是草稿里的词（研究者自己加的）返回 null。 */
export function reasonFor(draft: QueryDraft | null, kind: "include" | "exclude", term: string): string | null {
  const items = draft ? (kind === "include" ? draft.include : draft.exclude) : [];
  return items.find((item) => key(item.term) === key(term))?.reason ?? null;
}

export const splitList = (text: string) => text.split(/[,，;；\n]/).map((s) => s.trim()).filter(Boolean);

export function queryBody(editor: Editor): { body: QueryBody | null; error: string | null } {
  const include = editor.include
    .filter((row) => row.term.trim())
    .map((row) => ({ term: row.term.trim(), synonyms: splitList(row.synonyms) }));
  if (include.length === 0) return { body: null, error: "至少要有一个检索词。" };
  const exclude = editor.exclude.map((t) => t.trim()).filter(Boolean);
  const used = new Set(include.flatMap((row) => [row.term, ...row.synonyms]).map(key));
  const clash = exclude.find((t) => used.has(key(t)));
  if (clash) return { body: null, error: `排除词“${clash}”同时也是检索词，请删掉其中一个。` };
  const rows = editor.glossary.filter((row) => row.term.trim() || row.translation.trim() || row.definition.trim());
  const half = rows.find((row) => !row.term.trim() || !row.translation.trim());
  if (half) {
    return { body: null, error: half.term.trim() ? `术语“${half.term.trim()}”还没有译名，填上或删掉这一行。` : `有一条术语只填了译名“${half.translation.trim()}”，请补上原文写法或删掉这一行。` };
  }
  const glossary = rows.map((row) => ({ forms: splitList(row.term), translation: row.translation.trim(), definition: row.definition.trim() || null }));
  return { body: { include, exclude, glossary }, error: null };
}
