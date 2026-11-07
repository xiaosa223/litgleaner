// 本篇的段落译文：已缓存的、正在流式生成的、出错的。
import { useCallback, useEffect, useState } from "react";
import { api } from "./api";
import type { BlockTranslation } from "./types";

export interface Translations {
  done: Map<number, BlockTranslation>;
  live: Map<number, string>; // 正在生成的部分译文
  errors: Map<number, string>;
  translate: (index: number) => void;
  translateAll: (indices: number[]) => void; // 依次翻译，不同时发起多个请求
}

const without = <T,>(map: Map<number, T>, key: number) => {
  const next = new Map(map);
  next.delete(key);
  return next;
};

export function useTranslations(paperId: string, enabled: boolean, onSpent: () => void): Translations {
  const [done, setDone] = useState(new Map<number, BlockTranslation>());
  const [live, setLive] = useState(new Map<number, string>());
  const [errors, setErrors] = useState(new Map<number, string>());

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
  }, [paperId, enabled]);

  const run = useCallback(
    (index: number): Promise<void> => {
      setErrors((m) => without(m, index));
      setLive((m) => new Map(m).set(index, ""));
      return api
        .translate(paperId, index, (text) => setLive((m) => new Map(m).set(index, (m.get(index) ?? "") + text)))
        .then(
          (result) => setDone((m) => new Map(m).set(index, result)),
          (error: Error) => setErrors((m) => new Map(m).set(index, error.message)),
        )
        .finally(() => {
          setLive((m) => without(m, index));
          onSpent();
        });
    },
    [paperId, onSpent],
  );

  const translate = useCallback((index: number) => { if (!live.has(index)) void run(index); }, [live, run]);

  const translateAll = useCallback(
    (indices: number[]) => {
      void (async () => {
        for (const index of indices) await run(index);
      })();
    },
    [run],
  );

  return { done, live, errors, translate, translateAll };
}
