// 边读边问：针对选中的内容或整篇提问（第 12.4 节）。服务还没有接上时如实告诉读者，不编回答。
import { useEffect, useRef, useState } from "react";
import { api, isNotConnected } from "../api";

export interface QaMessage { role: "user" | "bot" | "system"; text: string; quote?: string | null }

export function QaTab({ paperId, messages, onMessages, quote, onQuote }: {
  paperId: string; messages: QaMessage[]; onMessages: (messages: QaMessage[]) => void;
  quote: string | null; onQuote: (quote: string | null) => void;
}) {
  const [draft, setDraft] = useState("");
  const [busy, setBusy] = useState(false);
  const end = useRef<HTMLDivElement>(null);
  const input = useRef<HTMLTextAreaElement>(null);
  // 新版浏览器的 scrollIntoView 会返回 Promise，effect 不能把它当清理函数返回
  useEffect(() => {
    void end.current?.scrollIntoView({ block: "end" });
  }, [messages.length]);
  useEffect(() => { if (quote) input.current?.focus(); }, [quote]);

  const send = async () => {
    const question = draft.trim();
    if (!question || busy) return;
    const asked: QaMessage[] = [...messages, { role: "user", text: question, quote }];
    onMessages(asked);
    setDraft("");
    onQuote(null);
    setBusy(true);
    try {
      const { answer } = await api.ask(paperId, question, quote);
      onMessages([...asked, { role: "bot", text: answer }]);
    } catch (error) {
      const text = isNotConnected(error)
        ? "边读边问的服务还没有接上，这个问题没有发出去。"
        : error instanceof Error ? error.message : "提问失败，请重试。";
      onMessages([...asked, { role: "system", text }]);
    } finally {
      setBusy(false);
    }
  };

  return (
    <>
      <div className="tabbody">
        <div className="preview-note" role="note">界面预览 · 边读边问的服务还没有接上，现在只能看交互。</div>
        {messages.length === 0 && (
          <div className="tag" style={{ fontSize: 12 }}>
            选中一段文字点“问”，或直接在下面提问。回答会引用原文并标明页码；没有原文依据的部分会标为“模型推断”。
          </div>
        )}
        <div className="msgs" aria-live="polite">
          {messages.map((m, i) => (
            <div key={i} className={`msg ${m.role}`}>
              {m.quote && <div className="msg-quote">“{m.quote}”</div>}
              {m.text}
            </div>
          ))}
          {busy && <div className="msg bot">正在回答…</div>}
          <div ref={end} />
        </div>
      </div>
      <div className="ask">
        {quote && (
          <div className="quote-chip">
            <span>“{quote}”</span>
            <button className="linkbtn" onClick={() => onQuote(null)} aria-label="去掉引用">×</button>
          </div>
        )}
        <div className="ask-row">
          <textarea id="askInput" ref={input} aria-label="提问" value={draft} placeholder={quote ? "针对这段提问" : "针对整篇提问"}
            onChange={(e) => setDraft(e.target.value)}
            onKeyDown={(e) => { if (e.key === "Enter" && !e.shiftKey) { e.preventDefault(); void send(); } }} />
          <button className="primary" disabled={!draft.trim() || busy} onClick={() => void send()}>问</button>
        </div>
      </div>
    </>
  );
}
