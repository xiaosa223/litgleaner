// 通用小部件：作者原话、模型内容、提示、弹窗。
import { createContext, useCallback, useContext, useEffect, useState, type ReactNode } from "react";

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

export function Modal({ children, onClose, width, title, top = false, bare = false }: {
  children: ReactNode; onClose: () => void; width?: number; title?: string;
  top?: boolean; // 靠上显示（检索）
  bare?: boolean; // 不带内边距和底部的关闭按钮，内容自己负责
}) {
  useEffect(() => {
    const onKey = (event: KeyboardEvent) => {
      if (event.key === "Escape") onClose();
    };
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, [onClose]);
  return (
    <div className={`modal${top ? " top" : ""}`} onClick={(e) => e.target === e.currentTarget && onClose()}>
      <div className={`box${bare ? " palette" : ""}`} style={width ? { maxWidth: width } : undefined} role="dialog" aria-modal="true" aria-label={title}>
        {title && !bare && <h3 className="dialog-title">{title}</h3>}
        {children}
        {!bare && !title && (
          <div style={{ textAlign: "right", marginTop: 12 }}>
            <button className="ghost" onClick={onClose}>关闭</button>
          </div>
        )}
      </div>
    </div>
  );
}

/** 带出处的作者原话：没有跳转目标时用（知识库、复习）。page 从 1 开始。 */
export function SourcedQuote({ quote, paper, page }: { quote: string; paper: string; page: number }) {
  return (
    <blockquote className="author">
      <span className="badge b-author">作者原话</span> “{quote}”
      <div className="tag" style={{ fontStyle: "normal", marginTop: 4 }}>{paper} · 第 {page} 页</div>
    </blockquote>
  );
}
