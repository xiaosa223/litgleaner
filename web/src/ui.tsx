// 通用小部件：作者原话、模型内容、提示、弹窗。
import { createContext, useCallback, useContext, useState, type ReactNode } from "react";

export function AuthorQuote({ quote, page, onJump, label = "作者原话" }: {
  quote: string; page: number | null; onJump?: () => void; label?: string | null;
}) {
  return (
    <blockquote className="author">
      {label && <><span className="badge b-author">{label}</span> </>}“{quote}”
      {page !== null && onJump && <button className="pg" onClick={onJump}>第 {page + 1} 页 ↗</button>}
    </blockquote>
  );
}

export function ModelText({ text, kind = "model" }: { text: string; kind?: "model" | "inferred" }) {
  return (
    <div className={kind === "inferred" ? "infer" : "model"}>
      <span className={`badge ${kind === "inferred" ? "b-infer" : "b-model"}`}>{kind === "inferred" ? "模型推断" : "模型概括"}</span>{" "}
      <span className="mt">{text}</span>
    </div>
  );
}

export function UserText({ text }: { text: string }) {
  return (
    <div className="model" style={{ borderColor: "var(--user)", background: "var(--user-soft)" }}>
      <span className="badge b-user">你</span> {text}
    </div>
  );
}

type Toast = (text: string) => void;
const ToastContext = createContext<Toast>(() => undefined);
export const useToast = () => useContext(ToastContext);

export function ToastProvider({ children }: { children: ReactNode }) {
  const [message, setMessage] = useState<{ text: string; id: number } | null>(null);
  const show = useCallback((text: string) => {
    const id = Date.now();
    setMessage({ text, id });
    window.setTimeout(() => setMessage((current) => (current?.id === id ? null : current)), 1900);
  }, []);
  return (
    <ToastContext.Provider value={show}>
      {children}
      {message && <div className="toast" role="status">{message.text}</div>}
    </ToastContext.Provider>
  );
}

export function Modal({ children, onClose, width }: { children: ReactNode; onClose: () => void; width?: number }) {
  return (
    <div className="modal" onClick={(e) => e.target === e.currentTarget && onClose()}>
      <div className="box" style={width ? { maxWidth: width } : undefined} role="dialog">
        {children}
        <div style={{ textAlign: "right", marginTop: 12 }}>
          <button className="ghost" onClick={onClose}>关闭</button>
        </div>
      </div>
    </div>
  );
}

/** 还没实现的功能：保留位置，说明原因。 */
export const SOON = {
  translate: "段落翻译还在开发中（路线图 5b）",
  facets: "分面高亮还在开发中（路线图 5d）",
  terms: "术语释义需要课题术语表，还在开发中",
  guide: "背景卡、章节要点和前置阅读还在开发中（路线图 5c）",
  ask: "边读边问还在开发中（路线图 5e）",
  card: "“先写后看”的读后卡片还在开发中（路线图 5f）",
  export: "导出组会提纲随读后卡片一起提供（路线图 5f）",
} as const;
