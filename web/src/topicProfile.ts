// 上传任务书等文档推断研究画像（ADR 0012）的纯逻辑：本地预检、错误提示、预填表单、自动起草标记。

export interface ProfileGuess { name: string; background: string; content: string; goals: string[] }
export interface NewTopicForm { name: string; background: string; content: string; goals: string }

export const MAX_DOCUMENT_BYTES = 20 * 1024 * 1024;
export const DOCUMENT_ACCEPT = ".pdf,.docx,.md,.markdown,.txt";
const SUFFIXES = [".pdf", ".docx", ".md", ".markdown", ".txt", ".text"];

export const profileForm = (guess: ProfileGuess): NewTopicForm => ({
  name: guess.name, background: guess.background, content: guess.content, goals: guess.goals.join("\n"),
});

/** 上传前在本地能看出来的问题；没有问题返回 null。 */
export function documentProblem(file: { name: string; size: number }): string | null {
  const name = file.name.toLowerCase();
  if (!SUFFIXES.some((suffix) => name.endsWith(suffix))) return "只支持 PDF、Word（.docx）、Markdown 和纯文本文件。旧版 .doc 请先另存为 .docx。";
  if (file.size === 0) return "文件是空的。";
  if (file.size > MAX_DOCUMENT_BYTES) return "文件超过 20 MB，请只保留正文部分后再试。";
  return null;
}

const MESSAGES: Record<string, string> = {
  document_no_text: "没有从文件里读到文字，可能是扫描件或图片。请上传带文字层的文件，或直接手动填写。",
  document_type_unsupported: "只支持 PDF、Word（.docx）、Markdown 和纯文本文件。",
  document_too_large: "文件内容过大，请只保留正文部分后再试。",
  document_unreadable: "文件可能已损坏，没能打开。",
};
export const documentError = (code: string): string => MESSAGES[code] ?? "没能读取这个文件，可以手动填写下面的内容。";

// 用文档新建的课题：进入课题页后自动起草检索词，用户不用再点一次。只在本标签页里有效，用过即删。
const flag = (id: string) => `autodraft:${id}`;
type Storage = { getItem(k: string): string | null; setItem(k: string, v: string): void; removeItem(k: string): void };
const storage = (): Storage | null => (globalThis as { sessionStorage?: Storage }).sessionStorage ?? null;

export function markAutoDraft(id: string): void {
  try { storage()?.setItem(flag(id), "1"); } catch { /* 存不了就让用户手动点起草 */ }
}

export function takeAutoDraft(id: string): boolean {
  try {
    const store = storage();
    if (!store || store.getItem(flag(id)) !== "1") return false;
    store.removeItem(flag(id));
    return true;
  } catch {
    return false;
  }
}
