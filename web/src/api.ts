// 所有请求同源发出；改变状态的请求带 CSRF 令牌（第 15.5 节）。
import type { ProfileGuess } from "./topicProfile";
import type { ConceptCard, ConceptCards, GuidePage, MasteryState, MergeView, MetadataField, MetadataView, Rating } from "./guidance";
import type { Identified } from "./identify";
import type { CatalogBody, CatalogResult, ModelBody, ModelSettingsView, ProbeResult } from "./modelForm";
import type { QaSubmission, QaView } from "./qa";
import type { DraftPreview, DraftStatus, DraftView, QueryBody } from "./topicQuery";
import type { BackfillPlan, ProfileBody, TopicDetail } from "./topicSettings";
import type { CountBody, TermCounts } from "./termCounts";
import type { MapPreview, MapStatus, MapView, ReadingRoute, SurveyPreview, SurveyRound, SurveyStatus, TopicSummary } from "./topicView";
import type {
  Annotation,
  BlockTranslation,
  DocumentView,
  Inbox,
  LibraryFilters,
  LibraryPage,
  NewAnnotation,
  NoteView,
  PaperView,
  ReadingStatus,
  SearchHit,
  TranslationPlan,
  AddedPaper,
} from "./types";

export class ApiError extends Error {
  readonly status: number;
  readonly code: string | null; // 服务端的稳定错误码，例如 model_configuration_required
  readonly task: string | null; // 与模型任务有关的错误带任务名
  constructor(status: number, message: string, code: string | null = null, task: string | null = null) {
    super(message);
    this.status = status;
    this.code = code;
    this.task = task;
  }
}

/** 第一次用到模型、还没有配置时返回任务名，调用方据此在原地引导配置。 */
export const missingModel = (error: unknown): string | null =>
  error instanceof ApiError && error.code === "model_configuration_required" ? error.task ?? "" : null;

let csrf: Promise<string> | null = null;

function csrfToken(): Promise<string> {
  csrf ??= get<{ csrf_token: string | null }>("/api/session").then((data) => {
    if (!data.csrf_token) throw new ApiError(401, "会话已失效，请重新用启动链接打开");
    return data.csrf_token;
  });
  return csrf;
}

async function parse<T>(response: Response): Promise<T> {
  if (!response.ok) {
    let message = `请求失败（${response.status}）`;
    let code: string | null = null;
    let task: string | null = null;
    try {
      const body = (await response.json()) as { detail?: unknown; code?: unknown; task?: unknown };
      if (typeof body.detail === "string") message = body.detail;
      if (typeof body.code === "string") code = body.code;
      if (typeof body.task === "string") task = body.task;
    } catch {
      // 响应不是 JSON 时用通用提示
    }
    throw new ApiError(response.status, message, code, task);
  }
  if (response.status === 204) return undefined as T;
  return (await response.json()) as T;
}

async function get<T>(path: string, signal?: AbortSignal): Promise<T> {
  return parse<T>(await fetch(path, { credentials: "same-origin", signal }));
}

async function send<T>(method: string, path: string, body?: unknown): Promise<T> {
  const headers: Record<string, string> = { "X-CSRF-Token": await csrfToken() };
  if (body !== undefined) headers["Content-Type"] = "application/json";
  const response = await fetch(path, {
    method,
    headers,
    credentials: "same-origin",
    body: body === undefined ? undefined : JSON.stringify(body),
  });
  return parse<T>(response);
}

const paperPath = (id: string) => `/api/papers/${encodeURIComponent(id)}`;

/** 界面先于后端做好的功能：接口还不存在时服务返回 404 或 405。 */
export const isNotConnected = (error: unknown): boolean =>
  error instanceof ApiError && [404, 405, 501].includes(error.status);

export const NOT_CONNECTED = "这项功能的服务还没有接上，现在只能看界面。";

/** 下载服务端生成的文件（POST + CSRF），返回文件内容，由页面触发保存。 */
async function download(path: string, body: unknown): Promise<Blob> {
  const response = await fetch(path, {
    method: "POST",
    headers: { "X-CSRF-Token": await csrfToken(), "Content-Type": "application/json" },
    credentials: "same-origin",
    body: JSON.stringify(body),
  });
  if (!response.ok) await parse<never>(response);
  return response.blob();
}

/** 上传文件：请求体就是文件本身；文件名和首页找到的标识符放在请求头里。 */
async function upload<T>(path: string, file: File, identifier: string | null, contentType = "application/pdf"): Promise<T> {
  const headers: Record<string, string> = {
    "X-CSRF-Token": await csrfToken(),
    "Content-Type": contentType,
    "X-File-Name": encodeURIComponent(file.name),
  };
  if (identifier) headers["X-Identifier"] = identifier;
  const response = await fetch(path, { method: "POST", headers, credentials: "same-origin", body: file });
  return parse<T>(response);
}

export interface TranslationHandlers {
  onDelta: (index: number, text: string) => void; // 一句完整的译文
  onThinking: () => void; // 模型在思考，还没有正文
  onBlock: (result: BlockTranslation) => void; // 一块的最终结果（校验后）
}

/** 读取翻译的 SSE 流：delta、status、block 逐个回调，end 结束，error 抛出。 */
async function streamTranslation(path: string, body: unknown, handlers: TranslationHandlers): Promise<void> {
  const headers: Record<string, string> = { "X-CSRF-Token": await csrfToken() };
  if (body !== undefined) headers["Content-Type"] = "application/json";
  const response = await fetch(path, {
    method: "POST",
    headers,
    credentials: "same-origin",
    body: body === undefined ? undefined : JSON.stringify(body),
  });
  if (!response.ok || !response.body) await parse<never>(response);
  const reader = response.body!.getReader();
  const decoder = new TextDecoder();
  let buffer = "";
  for (;;) {
    const { value, done } = await reader.read();
    buffer += decoder.decode(value, { stream: !done });
    const events = buffer.split("\n\n");
    buffer = events.pop() ?? "";
    for (const raw of events) {
      const name = /^event: (.+)$/m.exec(raw)?.[1];
      const data = /^data: (.+)$/m.exec(raw)?.[1];
      if (!name || !data) continue;
      const payload = JSON.parse(data) as { index?: number; text?: string; message?: string; code?: string; task?: string };
      if (name === "delta" && payload.index !== undefined) handlers.onDelta(payload.index, payload.text ?? "");
      if (name === "status") handlers.onThinking();
      if (name === "block") handlers.onBlock(payload as unknown as BlockTranslation);
      if (name === "end") return;
      if (name === "error") {
        const code = payload.code ?? null;
        const task = payload.task ?? (code === "model_configuration_required" ? "translate" : null);
        throw new ApiError(code === "model_configuration_required" ? 409 : 400, payload.message ?? "翻译失败", code, task);
      }
    }
    if (done) throw new ApiError(500, "翻译中断，请重试");
  }
}

export const api = {
  inbox: () => get<Inbox>("/api/inbox"),
  library: (filters: LibraryFilters = {}, signal?: AbortSignal) => {
    const query = new URLSearchParams();
    for (const [key, value] of Object.entries(filters)) {
      if (value !== undefined && value !== "") query.set(key, String(value));
    }
    return get<LibraryPage>(`/api/library?${query}`, signal);
  },
  search: (query: string, signal?: AbortSignal) =>
    get<{ hits: SearchHit[] }>(`/api/search?${new URLSearchParams({ q: query, limit: "30" })}`, signal),
  costs: (paperId?: string) =>
    get<{ month_usd: number; paper_usd: number | null }>(
      paperId ? `/api/costs?paper_id=${encodeURIComponent(paperId)}` : "/api/costs",
    ),
  translations: (id: string) => get<BlockTranslation[]>(`${paperPath(id)}/translations`),
  translationPlan: (id: string) => get<TranslationPlan>(`${paperPath(id)}/translation-plan`),
  confirmTranslationPlan: (id: string) => send<TranslationPlan>("POST", `${paperPath(id)}/translation-plan`),
  /** 翻译这一块所在的整段；同段的其他块也会收到结果。 */
  translate: (id: string, index: number, handlers: TranslationHandlers) =>
    streamTranslation(`${paperPath(id)}/blocks/${index}/translate`, undefined, handlers),
  /** 按页或整篇：按段落分批翻译，一段完成就回调。 */
  translateMany: (id: string, indices: number[], handlers: TranslationHandlers) =>
    streamTranslation(`${paperPath(id)}/translate`, { indices }, handlers),
  paper: (id: string) => get<PaperView>(paperPath(id)),
  openPaper: (id: string) => send<NonNullable<PaperView["reading"]>>("POST", `${paperPath(id)}/open`),
  document: (id: string) => get<DocumentView>(`${paperPath(id)}/document`),
  pdfUrl: (id: string) => `${paperPath(id)}/pdf`,
  setReading: (id: string, status: ReadingStatus, position?: Record<string, number | string>) =>
    send<void>("PUT", `${paperPath(id)}/reading`, { status, position: position ?? null }),
  addAnnotation: (id: string, item: NewAnnotation) =>
    send<Annotation>("POST", `${paperPath(id)}/annotations`, item),
  editAnnotation: (annotationId: string, body: string | null) =>
    send<void>("PATCH", `/api/annotations/${encodeURIComponent(annotationId)}`, { body }),
  removeAnnotation: (annotationId: string) =>
    send<void>("DELETE", `/api/annotations/${encodeURIComponent(annotationId)}`),
  /** 按 DOI、arXiv 编号或链接添加论文，返回论文编号。 */
  addPaper: (item: Identified) => send<AddedPaper>("POST", "/api/inbox/papers", { text: item.value }),
  uploadPaper: (file: File, identifier: string | null) => upload<AddedPaper>("/api/inbox/files", file, identifier),
  qaHistory: (id: string, signal?: AbortSignal) =>
    get<QaView[]>(`${paperPath(id)}/qa?limit=200`, signal),
  ask: (id: string, question: QaSubmission) =>
    send<QaView>("POST", `${paperPath(id)}/qa`, question),
  saveQa: (id: string, questionId: string) =>
    send<Annotation>("POST", `${paperPath(id)}/qa/${encodeURIComponent(questionId)}/annotation`),
  guide: (id: string, signal?: AbortSignal) => get<GuidePage>(`${paperPath(id)}/guide`, signal),
  /** 用户确认后才生成导读：plan_hash 对应确认时看到的输入和费用。 */
  generateGuide: (id: string, planHash: string) => send<GuidePage>("POST", `${paperPath(id)}/guide`, { plan_hash: planHash }),
  submitAnswer: (id: string, guideId: string, questionIndex: number, text: string, evaluate: boolean) =>
    send<GuidePage>("POST", `${paperPath(id)}/guide/${encodeURIComponent(guideId)}/answers`, { question_index: questionIndex, text, evaluate }),
  evaluateAnswer: (id: string, cardId: string) =>
    send<GuidePage>("POST", `${paperPath(id)}/answers/${encodeURIComponent(cardId)}/evaluate`),
  markEvaluationWrong: (id: string, cardId: string, reason: string) =>
    send<void>("POST", `${paperPath(id)}/answers/${encodeURIComponent(cardId)}/wrong`, { reason }),
  conceptCards: (query = "", topicId: string | null = null, signal?: AbortSignal) =>
    get<ConceptCards>(`/api/knowledge/cards?${new URLSearchParams({ q: query, limit: "200", ...(topicId ? { topic_id: topicId } : {}) })}`, signal),
  setMastery: (conceptId: string, level: number) =>
    send<MasteryState>("POST", `/api/knowledge/concepts/${encodeURIComponent(conceptId)}/level`, { level }),
  /** 个人数据导出：论文、笔记、回答卡片、概念和引用文件打成一个 zip。 */
  exportMine: (includePdfs: boolean) => download("/api/exports/mine", { include_pdfs: includePdfs }),
  metadata: (id: string) => get<MetadataView>(`${paperPath(id)}/metadata`),
  /** 用户修正元数据：只在显示处覆盖来源元数据（ADR 0011）；空串撤销该字段的修正。 */
  saveMetadata: (id: string, fields: Partial<Record<MetadataField, string>>) =>
    send<MetadataView>("PUT", `${paperPath(id)}/metadata`, fields),
  notes: (signal?: AbortSignal) => get<NoteView[]>("/api/notes?limit=200", signal),
  pendingMerges: () => get<MergeView[]>("/api/knowledge/merges"),
  /** 确认或删除模型提议的概念关系、疑似重复；只记一条决定，可以改主意。 */
  decideConcept: (target: string, decision: "confirmed" | "rejected") =>
    send<void>("POST", "/api/knowledge/decisions", { target, decision }),
  reviewDue: () => get<ConceptCard[]>("/api/knowledge/review?limit=50"),
  rateReview: (conceptId: string, rating: Rating) =>
    send<MasteryState>("POST", `/api/knowledge/concepts/${encodeURIComponent(conceptId)}/review`, { rating }),
  models: () => get<ModelSettingsView>("/api/settings/models"),
  /** 用草稿参数实际调用一次模型：会产生很少的费用，不保存任何东西。 */
  testModel: (body: ModelBody) => send<ProbeResult>("POST", "/api/settings/models/test", body),
  saveModel: (body: ModelBody) => send<ModelSettingsView>("PUT", "/api/settings/models", body),
  /** 按连接信息列出可用模型：只读列表接口，不计费、不保存（ADR 0013）。 */
  listModels: (body: CatalogBody) => send<CatalogResult>("POST", "/api/settings/models/list", body),
};

const topicPath = (id: string) => `/api/topics/${encodeURIComponent(id)}`;

/** 课题调研、阅读路线和课题地图（docs/SURVEY.md、READING-ROUTE.md、TOPIC-MAP.md）。 */
export const topicApi = {
  list: () => get<TopicSummary[]>("/api/topics"),
  detail: (id: string) => get<TopicDetail>(topicPath(id)),
  /** 只改名称、背景、内容、目标；返回保存后的定义（新目标有了编号），changed 表示之后的论文会按新描述筛选 */
  saveProfile: (id: string, body: ProfileBody) => send<{ topic: TopicDetail; changed: boolean }>("PUT", `${topicPath(id)}/profile`, body),
  activate: (id: string) => send<{ id: string; status: TopicSummary["status"] }>("POST", `${topicPath(id)}/activate`, {}),
  pause: (id: string) => send<{ id: string; status: TopicSummary["status"] }>("POST", `${topicPath(id)}/pause`, {}),
  /** 不调用模型，只数篇数、估初筛费用 */
  previewBackfill: (id: string) => send<BackfillPlan>("POST", `${topicPath(id)}/backfill/preview`, {}),
  startBackfill: (id: string, plan: string) => send<{ queued: number }>("POST", `${topicPath(id)}/backfill`, { plan }),
  draft: (id: string) => get<DraftView>(`${topicPath(id)}/draft`),
  /** 不调用模型，只算费用 */
  previewDraft: (id: string) => send<DraftPreview>("POST", `${topicPath(id)}/draft/preview`, {}),
  startDraft: (id: string, planHash: string) => send<DraftStatus>("POST", `${topicPath(id)}/draft`, { plan_hash: planHash }),
  draftJob: (id: string, key: string) => get<DraftStatus>(`${topicPath(id)}/draft/jobs/${encodeURIComponent(key)}`),
  /** 每个检索词近一年的篇数：不调用模型，每行一次来源检索 */
  termCounts: (id: string, body: CountBody) => send<TermCounts>("POST", `${topicPath(id)}/term-counts`, body),
  /** 写入检索词和术语表，并启用课题 */
  saveQuery: (id: string, body: QueryBody) => send<{ id: string; terms: number; status: TopicSummary["status"] }>("PUT", `${topicPath(id)}/query`, body),
  /** 上传任务书等文档，调用一次模型推断名称、背景、内容、目标；不写文件、不启用（ADR 0012） */
  profileFromDocument: (file: File) =>
    upload<ProfileGuess>("/api/topics/profile-from-document", file, null, "application/octet-stream"),
  /** 只写课题定义，不启用、不调用模型 */
  create: (topic: { name: string; background: string; content: string; goals: string[] }) =>
    send<{ id: string }>("POST", "/api/topics", topic),
  route: (id: string) => get<ReadingRoute>(`${topicPath(id)}/reading-route?limit_per_group=20`),
  rounds: (id: string) => get<SurveyRound[]>(`${topicPath(id)}/survey/rounds`),
  /** 会请求各来源（可能要一两分钟），不调用模型 */
  previewSurvey: (id: string) => send<SurveyPreview>("POST", `${topicPath(id)}/survey/preview`, {}),
  startSurvey: (id: string, planId: string) => send<SurveyStatus>("POST", `${topicPath(id)}/survey`, { plan_id: planId }),
  survey: (id: string, runId: string) => get<SurveyStatus>(`${topicPath(id)}/survey/runs/${encodeURIComponent(runId)}`),
  map: (id: string) => get<MapView>(`${topicPath(id)}/map`),
  previewMap: (id: string) => send<MapPreview>("POST", `${topicPath(id)}/map/preview`, {}),
  startMap: (id: string, planHash: string) => send<MapStatus>("POST", `${topicPath(id)}/map`, { plan_hash: planHash }),
  mapJob: (id: string, jobKey: string) => get<MapStatus>(`${topicPath(id)}/map/jobs/${encodeURIComponent(jobKey)}`),
};
