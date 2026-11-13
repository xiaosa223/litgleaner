// 只读轮询不会重新提交问题或触发模型调用；请求失败时保留幂等键。
import { useCallback, useEffect, useRef, useState } from "react";
import { api, ApiError, missingModel } from "./api";
import { mergeQa, qaWaiting, submission, type QaSelection, type QaSubmission, type QaView } from "./qa";

export function useQa(paperId: string) {
  const [views, setViews] = useState<QaView[]>([]);
  const [loading, setLoading] = useState(true);
  const [sending, setSending] = useState(false);
  const [readError, setReadError] = useState<string | null>(null);
  const [sendError, setSendError] = useState<string | null>(null);
  const [modelMissing, setModelMissing] = useState(false); // 提交被拒：还没有配置问答模型
  const [version, setVersion] = useState(0);
  const pending = useRef<QaSubmission | null>(null);
  const busy = useRef(false);
  const mounted = useRef(false);
  const reload = useCallback(() => setVersion((value) => value + 1), []);

  useEffect(() => {
    mounted.current = true;
    return () => { mounted.current = false; };
  }, []);

  useEffect(() => {
    const abort = new AbortController();
    let timer: ReturnType<typeof setTimeout> | undefined;
    const load = async () => {
      try {
        const rows = await api.qaHistory(paperId, abort.signal);
        if (abort.signal.aborted) return;
        setViews((current) => mergeQa(current, rows));
        setReadError(null);
        if (rows.some(qaWaiting)) timer = setTimeout(() => void load(), 2000);
      } catch (cause) {
        if (!abort.signal.aborted) setReadError(cause instanceof Error ? cause.message : "问答状态读取失败");
      } finally {
        if (!abort.signal.aborted) setLoading(false);
      }
    };
    void load();
    return () => { abort.abort(); clearTimeout(timer); };
  }, [paperId, version]);

  const send = async (text: string, selected: QaSelection | null): Promise<boolean> => {
    if (busy.current) return false;
    busy.current = true;
    setSending(true);
    setSendError(null);
    setModelMissing(false);
    const request = submission(text, selected, pending.current);
    pending.current = request;
    try {
      const view = await api.ask(paperId, request);
      if (!mounted.current) return false;
      setViews((current) => mergeQa(current, [view]));
      pending.current = null;
      reload();
      return true;
    } catch (cause) {
      if (mounted.current && missingModel(cause) !== null) {
        setSendError("还没有配置问答模型，配好后会自动提交这个问题。");
        setModelMissing(true);
      } else if (mounted.current) {
        const rejected = cause instanceof ApiError && cause.status >= 400 && cause.status < 500;
        setSendError((cause instanceof Error ? cause.message : "提问未能确认") +
          (rejected ? "" : "；请再次点击提问，同一请求会复用，避免重复计费。"));
      }
      return false;
    } finally {
      busy.current = false;
      if (mounted.current) setSending(false);
    }
  };
  return { views, loading, sending, modelMissing, error: sendError ?? readError, reload, send };
}
