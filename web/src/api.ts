// 所有请求同源发出；改变状态的请求带 CSRF 令牌（第 15.5 节）。
import type {
  Annotation,
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

export const api = {
  inbox: () => get<Inbox>("/api/inbox"),
  costs: () => get<{ month_usd: number }>("/api/costs"),
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
