// 本篇的译文：已缓存的、正在生成的、出错的。翻译以段落为单位，一次请求可能返回同段的几块。
import { useCallback, useEffect, useRef, useState } from "react";
import { api, missingModel, type TranslationHandlers } from "./api";
import { useModelSetup } from "./ModelSetup";
import type { BlockTranslation } from "./types";

export interface Translations {
  done: Map<number, BlockTranslation>;
  live: Map<number, string>; // 正在生成：已到的整句译文，空串表示还没有
  thinking: boolean; // 模型在思考，还没有正文
  errors: Map<number, string>;
  translate: (index: number) => void;
  translateAll: (indices: number[]) => void; // 按段落分批，一次请求
  reload: () => void; // 重新读缓存（后台整篇翻译的进度）
}

const without = <T,>(map: Map<number, T>, keys: Iterable<number>) => {
  const next = new Map(map);
  for (const key of keys) next.delete(key);
  return next;
};

const withAll = <T,>(map: Map<number, T>, keys: Iterable<number>, value: T) => {
  const next = new Map(map);
  for (const key of keys) next.set(key, value);
  return next;
};

export function useTranslations(paperId: string, enabled: boolean, onSpent: () => void): Translations {
  const [done, setDone] = useState(new Map<number, BlockTranslation>());
  const [live, setLive] = useState(new Map<number, string>());
  const [errors, setErrors] = useState(new Map<number, string>());
  const [thinking, setThinking] = useState(false);
  const running = useRef(new Set<number>()); // 已发出请求、还没结束的块，避免重复请求
  const requireModel = useModelSetup();
  // 配好模型后重发同一个请求；run 依赖自身，所以经由引用调用
  const runRef = useRef<(indices: number[], request: (handlers: TranslationHandlers) => Promise<void>) => void>(() => undefined);

  const [version, setVersion] = useState(0);
  const reload = useCallback(() => setVersion((v) => v + 1), []);

  useEffect(() => {
    if (!enabled) return;
    let alive = true;
    api.translations(paperId).then(
      (items) => alive && setDone(new Map(items.map((t) => [t.index, t]))),
      () => undefined, // 读不到缓存不影响阅读，点“译”时再翻
    );
    return () => {
      alive = false;
    };
  }, [paperId, enabled, version]);

  const run = useCallback(
    (indices: number[], request: (handlers: TranslationHandlers) => Promise<void>) => {
      const fresh = indices.filter((i) => !running.current.has(i));
      if (fresh.length === 0) return;
      const touched = new Set(fresh);
      fresh.forEach((i) => running.current.add(i));
      setErrors((m) => without(m, fresh));
      setLive((m) => withAll(m, fresh, ""));
      const handlers: TranslationHandlers = {
        onThinking: () => setThinking(true),
        onDelta: (index, text) => {
          touched.add(index);
          setThinking(false);
          setLive((m) => new Map(m).set(index, (m.get(index) ? `${m.get(index)} ` : "") + text));
        },
        onBlock: (result) => {
          touched.add(result.index);
          setDone((m) => new Map(m).set(result.index, result));
          setLive((m) => without(m, [result.index]));
        },
      };
      request(handlers)
        .catch((error: Error) => {
          const task = missingModel(error);
          if (task === null) {
            setErrors((m) => withAll(m, fresh, error.message));
            return;
          }
          setErrors((m) => withAll(m, fresh, "还没有配置翻译模型"));
          requireModel(task || "translate", () => runRef.current(indices, request));
        })
        .finally(() => {
          touched.forEach((i) => running.current.delete(i));
          setLive((m) => without(m, touched));
          setThinking(false);
          onSpent();
        });
    },
    [onSpent, requireModel],
  );
  runRef.current = run;

  const translate = useCallback(
    (index: number) => run([index], (handlers) => api.translate(paperId, index, handlers)),
    [paperId, run],
  );

  const translateAll = useCallback(
    (indices: number[]) => run(indices, (handlers) => api.translateMany(paperId, indices, handlers)),
    [paperId, run],
  );

  return { done, live, thinking, errors, translate, translateAll, reload };
}
