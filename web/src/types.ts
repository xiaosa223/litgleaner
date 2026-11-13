// 与 src/litgleaner/application/reader.py 中的数据类一一对应。

export type ReadingStatus = "queued" | "reading" | "read" | "abandoned";

export interface InboxItem {
  paper_id: string;
  title: string;
  authors: string[];
  venue: string | null;
  published_at: string | null;
  doi: string | null;
  score: number | null; // 没推送过的论文（手动加入待读）没有分数和主题
  tier_label: string | null;
  provisional: boolean;
  summary: string | null;
  topic_id: string | null;
  topic_name: string | null;
  pushed_at: string | null;
  reading_status: ReadingStatus | null;
}

export interface TopicGoal {
  id: string;
  text: string;
}

export interface TopicProfile {
  id: string;
  name: string;
  goals: TopicGoal[];
}

export interface Inbox {
  recommended: InboxItem[];
  queued: InboxItem[];
  reading: InboxItem[];
  topics: TopicProfile[];
}

/** 文库来自完整的服务端查询，不受首页最近推荐条数限制。 */
export interface LibraryItem extends InboxItem {
  topic_ids: string[];
}

export interface LibraryFilters {
  status?: ReadingStatus | "new" | "all";
  topic_id?: string;
  q?: string;
  limit?: number;
  offset?: number;
}

export interface LibraryPage {
  items: LibraryItem[];
  total: number;
  counts: Record<ReadingStatus | "new" | "all", number>;
  topics: TopicProfile[];
}

export interface SearchHit {
  kind: "paper" | "fulltext" | "note" | "highlight" | "extraction" | "qa" | "concept";
  id: string;
  paper_id: string;
  title: string;
  text: string;
  page: number | null; // 零基页码，跳回原文时保留
  authorship: "author" | "user" | "model";
}

/** 页面归一化坐标 0–1000，原点在左上：x0, y0, x1, y1 */
export type BBox = [number, number, number, number];
/** 页面归一化坐标 0–1000：x, y, 宽, 高 */
export type Rect = [number, number, number, number];

export interface Annotation {
  id: string;
  paper_id: string;
  kind: "highlight" | "note";
  page: number | null;
  bbox: BBox | null;
  quote: string | null;
  body: string | null;
  created_at: string;
  updated_at: string;
  rects: Rect[];
  qa_question_id?: string | null;
}

export interface ReadingState {
  paper_id: string;
  status: ReadingStatus;
  position: Record<string, number | string> | null;
  updated_at: string;
}

export interface AssessmentEntry {
  score: number;
  tier: string;
  tier_label: string;
  topic_name: string;
  provisional: boolean;
  undetermined: string[];
  stale: boolean;
}

export interface PaperView {
  paper_id: string;
  title: string;
  abstract: string | null;
  authors: string[];
  venue: string | null;
  published_at: string | null;
  doi: string | null;
  identifiers: Record<string, string>;
  fulltext_status: string | null;
  parse_quality: string | null;
  has_pdf: boolean;
  has_document: boolean;
  extraction: Record<string, unknown> | null;
  assessments: Record<string, AssessmentEntry>;
  reading: ReadingState | null;
  annotations: Annotation[];
}

export interface Block {
  index: number;
  kind: string;
  text: string;
  page: number;
  bbox: BBox | null;
  caption: string | null;
  translatable: boolean;
}

/** 一段的译文：sources 与 sentences 按句一一对应。 */
/** 用户添加的一篇论文（第 12.8 节）。对上论文时 paper_id 不为空；error 也可能是“元数据待补”这样的说明。 */
export interface AddedPaper {
  id: string;
  kind: "doi" | "arxiv" | "url" | "pdf";
  value: string;
  status: "pending" | "done" | "failed";
  paper_id: string | null;
  error: string | null;
}

/** 整篇翻译的估算和进度（第 12.3 节）。估算不调用模型。 */
export interface TranslationPlan {
  paragraphs: number;
  pending: number;
  characters: number;
  estimated_usd: number | null; // 不知道模型价格时为 null
  state: "idle" | "queued" | "retrying" | "done" | "failed";
  error: string | null;
}

export interface BlockTranslation {
  index: number;
  sources: string[];
  sentences: string[];
  verified: boolean; // 通过句数和术语表校验；未通过的不缓存
  cached: boolean;
  stale: boolean; // 术语表或模型更新前的旧译文
  problems: string[];
}

export interface DocumentView {
  quality: string;
  body_start: number; // 正文从这一块开始（第一个摘要标记）
  blocks: Block[];
}

export interface NewAnnotation {
  kind: "highlight" | "note";
  page?: number | null;
  bbox?: BBox | null;
  quote?: string | null;
  body?: string | null;
  rects?: Rect[];
}

// ---- 以下是知识库的界面数据结构（ARCHITECTURE 第 12.7 节）。
// 掌握程度、复习等还没有接口，目前只有界面预览的示例数据使用；接口实现时以这里为约定的起点。

/** 掌握程度：0 没见过，1 见过，2 能解释，3 能应用 */
export type Mastery = 0 | 1 | 2 | 3;

export interface Concept {
  id: string;
  name: string; // 原文术语
  translation: string;
  level: Mastery;
  due: boolean; // 到了复习时间
  papers: number;
  definition: { quote: string; paper: string; page: number } | null;
  answer: string | null; // 用户自己写过的回答
  related: { kind: "前置" | "相似" | "对比"; name: string }[];
}

export interface NoteEntry {
  id: string;
  paper: string;
  page: number;
  quote: string;
  body: string | null;
}
