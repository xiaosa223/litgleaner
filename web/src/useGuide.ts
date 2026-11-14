// 导读与回答卡片的状态：只读轮询不会触发模型调用；生成和评价都由用户点击确认。
// 操作错误与读取错误分开保存：轮询成功不会抹掉刚才那次操作的失败原因。
import { useCallback, useEffect, useRef, useState } from "react";
import { api, missingModel } from "./api";
import { guideWaiting, type GuidePage } from "./guidance";
import { useModelSetup } from "./ModelSetup";

const FAST_POLL = 1500; // 生成或评价排队中
const SLOW_POLL = 5000; // 解析、提炼还在进行，或者上次读取失败

export function useGuide(paperId: string) {
  const [page, setPage] = useState<GuidePage | null>(null);
  const [readError, setReadError] = useState<string | null>(null);
  const [actionError, setActionError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);
  const [version, setVersion] = useState(0);
  const requireModel = useModelSetup();
  const alive = useRef(true);
  const current = useRef(paperId); // 切换论文后，迟到的响应一律丢弃
  const pageRef = useRef<GuidePage | null>(null);
  const inFlight = useRef(false);
  const reload = useCallback(() => setVersion((value) => value + 1), []);

  useEffect(() => {
    alive.current = true;
    return () => { alive.current = false; };
  }, []);

  useEffect(() => {
    current.current = paperId;
    pageRef.current = null;
    setPage(null);
    setReadError(null);
    setActionError(null);
  }, [paperId]);

  useEffect(() => {
    const abort = new AbortController();
    let timer: ReturnType<typeof setTimeout> | undefined;
    const load = async () => {
      try {
        const next = await api.guide(paperId, abort.signal);
        if (abort.signal.aborted) return;
        pageRef.current = next;
        setPage(next);
        setReadError(null);
        if (guideWaiting(next)) timer = setTimeout(() => void load(), FAST_POLL);
        else if (next.view.unavailable && !next.view.guide) timer = setTimeout(() => void load(), SLOW_POLL);
      } catch (cause) {
        if (abort.signal.aborted) return;
        setReadError(cause instanceof Error ? cause.message : "导读读取失败");
        timer = setTimeout(() => void load(), SLOW_POLL); // 网络抖动后自动恢复
      }
    };
    void load();
    return () => { abort.abort(); clearTimeout(timer); };
  }, [paperId, version]);

  /** 执行会调用模型的操作；缺模型配置时原地弹出配置，保存后用最新状态重试。 */
  const run = useCallback(async (action: () => Promise<GuidePage | void>, retry: () => void): Promise<boolean> => {
    if (inFlight.current) return false;
    inFlight.current = true;
    const target = current.current;
    setBusy(true);
    setActionError(null);
    try {
      const next = await action();
      if (!alive.current || current.current !== target) return false;
      if (next) { pageRef.current = next; setPage(next); }
      reload();
      return true;
    } catch (cause) {
      if (!alive.current || current.current !== target) return false;
      const task = missingModel(cause);
      if (task !== null) {
        requireModel(task || "guide", () => { if (alive.current && current.current === target) retry(); });
      } else {
        setActionError(cause instanceof Error ? cause.message : "操作失败，请重试");
        reload();
      }
      return false;
    } finally {
      inFlight.current = false;
      if (alive.current) setBusy(false);
    }
  }, [reload, requireModel]);

  // 以下操作在调用时读取最新状态（pageRef），配置模型后重试也不会用到旧的计划指纹
  const generate = useCallback(function generate(): void {
    const plan = pageRef.current?.view.plan;
    if (!plan?.configured || !plan.plan_hash) {
      requireModel("guide", () => { if (alive.current) reload(); });
      return;
    }
    const hash = plan.plan_hash;
    void run(() => api.generateGuide(current.current, hash), generate);
  }, [reload, requireModel, run]);

  const answer = useCallback(function answer(index: number, text: string, evaluate: boolean): Promise<boolean> {
    const guide = pageRef.current?.view.guide;
    if (!guide || guide.paper_id !== current.current) return Promise.resolve(false);
    return run(() => api.submitAnswer(guide.paper_id, guide.id, index, text, evaluate), () => void answer(index, text, evaluate));
  }, [run]);

  const evaluate = useCallback(function evaluate(cardId: string): void {
    void run(() => api.evaluateAnswer(current.current, cardId), () => evaluate(cardId));
  }, [run]);

  return { page, error: actionError ?? readError, busy, reload, generate, answer, evaluate };
}
