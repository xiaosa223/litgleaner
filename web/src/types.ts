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
  topics: TopicProfile[];
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
}

export interface DocumentView {
  quality: string;
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
