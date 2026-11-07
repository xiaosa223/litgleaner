// 所有请求同源发出；改变状态的请求带 CSRF 令牌（第 15.5 节）。
import type {
  Annotation,
  BlockTranslation,
  DocumentView,
  Inbox,
  NewAnnotation,
  PaperView,
  ReadingStatus,
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

async function get<T>(path: string): Promise<T> {
  return parse<T>(await fetch(path, { credentials: "same-origin" }));
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

/** 读取翻译的 SSE 流：delta 逐段回调，done 返回结果，error 抛出。 */
async function streamTranslation(path: string, onDelta: (text: string) => void): Promise<BlockTranslation> {
  const response = await fetch(path, { method: "POST", headers: { "X-CSRF-Token": await csrfToken() }, credentials: "same-origin" });
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
      const payload = JSON.parse(data) as { text?: string; message?: string };
      if (name === "delta") onDelta(payload.text ?? "");
      if (name === "done") return payload as unknown as BlockTranslation;
      if (name === "error") throw new ApiError(400, payload.message ?? "翻译失败");
    }
    if (done) throw new ApiError(500, "翻译中断，请重试");
  }
}

export const api = {
  inbox: () => get<Inbox>("/api/inbox"),
  costs: (paperId?: string) =>
    get<{ month_usd: number; paper_usd: number | null }>(
      paperId ? `/api/costs?paper_id=${encodeURIComponent(paperId)}` : "/api/costs",
    ),
  translations: (id: string) => get<BlockTranslation[]>(`${paperPath(id)}/translations`),
  translate: (id: string, index: number, onDelta: (text: string) => void) =>
    streamTranslation(`${paperPath(id)}/blocks/${index}/translate`, onDelta),
  paper: (id: string) => get<PaperView>(paperPath(id)),
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
};
