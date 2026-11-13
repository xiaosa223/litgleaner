// 持久问答：提问排队、轮询历史，作者原文和模型推断分别展示。
import { useEffect, useRef, useState } from "react";
import { api } from "../api";
import { qaStatus, type QaCitation, type QaSelection } from "../qa";
import { useQa } from "../useQa";
import { useToast } from "../ui";
import { useModelSetup } from "../ModelSetup";
import { QaAnswer } from "./QaAnswer";

export function QaTab({ paperId, selected, onSelection, onJump, onChange, onAnswered, savedQuestions }: {
  paperId: string; selected: QaSelection | null; onSelection: (value: QaSelection | null) => void;
  onJump: (citation: QaCitation) => void; onChange: () => void;
  onAnswered: () => void; savedQuestions: string[];
}) {
  const [draft, setDraft] = useState("");
  const { views, loading, sending, modelMissing, error, reload, send } = useQa(paperId);
  const [saving, setSaving] = useState<string | null>(null);
  const [saved, setSaved] = useState<Set<string>>(new Set());
  const toast = useToast();
  const requireModel = useModelSetup();
  const needsModel = views.some((view) => view.state === "configuration_required");
  const lastState = views[views.length - 1]?.state;
  const prompted = useRef(false);
  // 第一次提问就遇到没有配置模型：直接在原地弹出配置；配好后排队的问题会自动继续
  useEffect(() => {
    if (lastState !== "configuration_required") prompted.current = false; // 下次再遇到时还会提示
    else if (!prompted.current) {
      prompted.current = true;
      requireModel("ask", reload);
    }
  }, [lastState, requireModel, reload]);
  const end = useRef<HTMLDivElement>(null);
  const input = useRef<HTMLTextAreaElement>(null);
  const currentSelection = useRef(selected);
  currentSelection.current = selected;
  useEffect(() => { if (selected) input.current?.focus(); }, [selected]);
  const answered = views.filter((view) => view.answer).length;
  useEffect(() => { void end.current?.scrollIntoView({ block: "nearest" }); }, [views.length, answered]);
  useEffect(() => { if (answered) onAnswered(); }, [answered, onAnswered]);

  const ask = async () => {
    if (!draft.trim() || sending) return;
    if (await send(draft, selected)) {
      setDraft("");
      if (currentSelection.current === selected) onSelection(null);
    }
  };
  const askRef = useRef(ask);
  askRef.current = ask;
  // 提交时还没有配置模型：原地弹出配置，配好后用同一请求编号重新提交，不会重复计费
  const alive = useRef(true);
  useEffect(() => () => { alive.current = false; }, []);
  useEffect(() => {
    // 配好时如果已经离开这篇论文，不再替用户提交
    if (modelMissing) requireModel("ask", () => { if (alive.current) void askRef.current(); });
  }, [modelMissing, requireModel]);
  const save = async (id: string) => {
    setSaving(id);
    try {
      await api.saveQa(paperId, id);
      setSaved((current) => new Set([...current, id]));
      onChange();
      toast("已保存关联笔记，保留问题、回答及原文出处");
    } catch (cause) { toast(cause instanceof Error ? cause.message : "保存失败"); }
    finally { setSaving(null); }
  };

  return (
    <>
      <div className="tabbody">
        <div className="qa-toolbar"><span className="tag">最近 200 个问题 · 回答附原文出处</span><button className="linkbtn" onClick={reload}>刷新状态</button></div>
        {loading && <div role="status" className="tag">正在读取问答记录…</div>}
        {!loading && !views.length && <p className="tag">选中文字点“问”，或针对整篇提问。有原文依据的回答附页码；证据不足的判断标为模型推断。</p>}
        {error && <div className="msg system" role="alert">{error}</div>}
        {needsModel && (
          <div className="msg system" role="note">
            还没有配置问答模型，问题已保存，配好后自动回答。{" "}
            <button className="linkbtn" onClick={() => requireModel("ask", reload)}>现在配置</button>
          </div>
        )}
        <div className="msgs">
          {views.map((view) => (
            <section className="qa-turn" key={view.question.id} aria-label="问答记录">
              <div className="msg user">
                {view.question.selection && <div className="msg-quote">“{view.question.selection}”{view.question.page !== null && <span> · 第 {view.question.page + 1} 页</span>}</div>}
                {view.question.text}
              </div>
              {view.answer ? <>
                <QaAnswer answer={view.answer} paperId={paperId} onJump={onJump} />
                <button className="ghost" disabled={saving !== null || saved.has(view.question.id) || savedQuestions.includes(view.question.id)} onClick={() => void save(view.question.id)}>
                  {saved.has(view.question.id) || savedQuestions.includes(view.question.id) ? "已保存关联笔记" : saving === view.question.id ? "正在保存…" : "保存关联笔记"}
                </button>
              </> : <div className={`msg ${view.state === "failed" ? "system" : "bot"}`} role="status">{qaStatus(view)}</div>}
            </section>
          ))}
          <div ref={end} />
        </div>
      </div>
      <div className="ask">
        {selected && <div className="quote-chip"><span>“{selected.text}”{selected.page !== null && ` · 第 ${selected.page + 1} 页`}</span><button className="linkbtn" disabled={sending} onClick={() => onSelection(null)} aria-label="去掉引用">×</button></div>}
        <div className="ask-row">
          <textarea ref={input} aria-label="提问" value={draft} disabled={sending} maxLength={4000} placeholder={selected ? "针对这段提问" : "针对整篇提问"}
            onChange={(event) => setDraft(event.target.value)}
            onKeyDown={(event) => { if (event.key === "Enter" && !event.shiftKey && !event.nativeEvent.isComposing) { event.preventDefault(); void ask(); } }} />
          <button className="primary" disabled={!draft.trim() || sending} onClick={() => void ask()}>{sending ? "提交中…" : "问"}</button>
        </div>
        <div className="tag">提问会使用模型额度；刷新记录和查看引用不调用模型。</div>
      </div>
    </>
  );
}
