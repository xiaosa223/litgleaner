// 所有请求同源发出；改变状态的请求带 CSRF 令牌（第 15.5 节）。
import type { Identified } from "./identify";
import type {
  Annotation,
  BlockTranslation,
  DocumentView,
  Inbox,
  LibraryFilters,
  LibraryPage,
  NewAnnotation,
  PaperView,
  ReadingStatus,
  SearchHit,
  TranslationPlan,
  AddedPaper,
} from "./types";

export class ApiError extends Error {
  constructor(
    readonly status: number,
    message: string,
  ) {
    super(message);
  }
}

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
    try {
      const body = (await response.json()) as { detail?: unknown };
      if (typeof body.detail === "string") message = body.detail;
    } catch {
      // 响应不是 JSON 时用通用提示
    }
    throw new ApiError(response.status, message);
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

/** 上传 PDF：请求体就是文件本身；文件名和首页找到的标识符放在请求头里。 */
async function upload<T>(path: string, file: File, identifier: string | null): Promise<T> {
  const headers: Record<string, string> = {
    "X-CSRF-Token": await csrfToken(),
    "Content-Type": "application/pdf",
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
      const payload = JSON.parse(data) as { index?: number; text?: string; message?: string };
      if (name === "delta" && payload.index !== undefined) handlers.onDelta(payload.index, payload.text ?? "");
      if (name === "status") handlers.onThinking();
      if (name === "block") handlers.onBlock(payload as unknown as BlockTranslation);
      if (name === "end") return;
      if (name === "error") throw new ApiError(400, payload.message ?? "翻译失败");
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
  // ---- 下面的接口后端还没有实现（ARCHITECTURE 第 7.5、12.4 节），调用方用 isNotConnected 处理 ----
  createTopic: (topic: { name: string; background: string; content: string; goals: string[] }) =>
    send<{ id: string }>("POST", "/api/topics", topic),
  startSurvey: (topicId: string) => send<void>("POST", `/api/topics/${encodeURIComponent(topicId)}/survey`),
  ask: (id: string, question: string, quote: string | null) =>
    send<{ answer: string }>("POST", `${paperPath(id)}/ask`, { question, quote }),
};
