// 页面地址和解析：纯函数，不依赖浏览器（第 12.8 节的页面结构）。
export type Route =
  | { name: "today" }
  | { name: "topics" }
  | { name: "topic"; topicId: string }
  | { name: "library" }
  | { name: "knowledge" }
  | { name: "settings" }
  | { name: "paper"; paperId: string };

const enc = encodeURIComponent;

export const paths = {
  today: "/",
  topics: "/topics",
  topic: (id: string) => `/topics/${enc(id)}`,
  library: "/library",
  knowledge: "/knowledge",
  settings: "/settings",
  paper: (id: string) => `/papers/${enc(id)}`,
} as const;

function decode(value: string): string | null {
  try {
    return decodeURIComponent(value);
  } catch {
    return null; // 坏的转义：按未知地址处理
  }
}

export function parseRoute(pathname: string): Route {
  const [head, raw, extra] = pathname.split("/").filter(Boolean);
  if (extra !== undefined) return { name: "today" };
  const id = raw === undefined ? null : decode(raw);
  if (raw !== undefined) {
    if (id === null) return { name: "today" };
    if (head === "papers") return { name: "paper", paperId: id };
    if (head === "topics") return { name: "topic", topicId: id };
    return { name: "today" };
  }
  if (head === "topics" || head === "library" || head === "knowledge" || head === "settings") return { name: head };
  return { name: "today" };
}
