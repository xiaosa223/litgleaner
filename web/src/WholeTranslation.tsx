// 整篇翻译（第 12.3 节）：先估算费用，用户确认后排入后台任务；翻译过程中按段显示，可以继续阅读。
import { useCallback, useEffect, useState } from "react";
import { api, missingModel } from "./api";
import { useModelSetup } from "./ModelSetup";
import type { TranslationPlan } from "./types";
import { Modal } from "./ui";

const POLL_MS = 3000;
const running = (plan: TranslationPlan | null) => plan?.state === "queued" || plan?.state === "retrying";

/** 后台整篇翻译的进度。排队或进行中时每 3 秒查一次，并让译文重新读缓存。 */
export function useWholeTranslation(paperId: string, enabled: boolean, onProgress: () => void) {
  const [plan, setPlan] = useState<TranslationPlan | null>(null);
  const [failure, setFailure] = useState<{ needsModel: boolean; message: string } | null>(null);
  // 没有可翻译的正文（还没解析完）时不查，免得接口报错
  const refresh = useCallback(() => {
    if (!enabled) {
      setPlan(null);
      setFailure(null);
      return Promise.resolve();
    }
    return api.translationPlan(paperId).then(
      (next) => { setPlan(next); setFailure(null); },
      (error: unknown) => {
        setPlan(null);
        setFailure({
          needsModel: missingModel(error) !== null,
          message: error instanceof Error ? error.message : "估算失败",
        });
      },
    );
  }, [paperId, enabled]);
  useEffect(() => void refresh(), [refresh]);
  useEffect(() => {
    if (!running(plan)) return;
    const timer = window.setTimeout(() => {
      onProgress();
      void refresh();
    }, POLL_MS);
    return () => window.clearTimeout(timer);
  }, [plan, refresh, onProgress]);
  useEffect(() => {
    if (plan?.state === "done") onProgress(); // 最后一批译文
  }, [plan?.state, onProgress]);
  const confirm = useCallback(async () => setPlan(await api.confirmTranslationPlan(paperId)), [paperId]);
  return { plan, failure, refresh, confirm, running: running(plan) };
}

function cost(plan: TranslationPlan): string {
  if (plan.estimated_usd === null) return "无法估算费用：模型价目表里没有这个模型的价格";
  return `预计费用约 $${plan.estimated_usd < 0.01 ? plan.estimated_usd.toFixed(4) : plan.estimated_usd.toFixed(2)}（按价目表和字数估算，实际以记录为准）`;
}

export function WholeTranslationDialog({ plan, failure, onConfirm, onRefresh, onClose }: {
  plan: TranslationPlan | null; failure: { needsModel: boolean; message: string } | null;
  onConfirm: () => Promise<void>; onRefresh: () => Promise<void>; onClose: () => void;
}) {
  const [error, setError] = useState<string | null>(null);
  const [sending, setSending] = useState(false);
  const requireModel = useModelSetup();
  const start = async (): Promise<void> => {
    setSending(true);
    try {
      await onConfirm();
      onClose();
    } catch (e) {
      const task = missingModel(e);
      if (task !== null) {
        setError("还没有配置翻译模型，配好后会自动开始。");
        requireModel(task || "translate", () => void start());
        return;
      }
      setError(e instanceof Error ? e.message : "排队失败");
    } finally {
      setSending(false);
    }
  };
  return (
    <Modal title="翻译全文" onClose={onClose} width={440}>
      {!plan && failure?.needsModel ? (
        <p>翻译全文需要先配置翻译模型。配好后会重新估算费用，确认后才开始翻译。{" "}
          <button className="linkbtn" onClick={() => requireModel("translate", () => void onRefresh())}>现在配置</button>
        </p>
      ) : !plan && failure ? (
        <p style={{ color: "var(--warn)" }}>{failure.message}</p>
      ) : !plan ? <p>正在估算…</p> : plan.pending === 0 ? (
        <p>全文 {plan.paragraphs} 段都已经有当前版本的译文。</p>
      ) : running(plan) ? (
        <p>后台正在翻译：还剩 {plan.pending} / {plan.paragraphs} 段。可以关掉这个窗口继续阅读，译文会陆续出现。</p>
      ) : (
        <>
          <p>还有 {plan.pending} / {plan.paragraphs} 段没有译文，约 {plan.characters.toLocaleString()} 字。</p>
          <p className="d">{cost(plan)}</p>
          {plan.state === "failed" && plan.error && <p style={{ color: "var(--warn)" }}>上次没有完成：{plan.error}</p>}
          <p className="d">确认后在后台按段落翻译，已有的译文不会重复翻译、不重复收费。</p>
        </>
      )}
      {error && <p style={{ color: "var(--warn)" }}>{error}</p>}
      <div style={{ display: "flex", gap: 8, justifyContent: "flex-end", marginTop: 12 }}>
        <button className="ghost" onClick={onClose}>{plan && plan.pending > 0 && !running(plan) ? "取消" : "关闭"}</button>
        {plan && plan.pending > 0 && !running(plan) && (
          <button className="primary" disabled={sending} onClick={() => void start()}>开始翻译</button>
        )}
      </div>
    </Modal>
  );
}
