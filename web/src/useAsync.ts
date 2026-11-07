import { useCallback, useEffect, useState } from "react";

export type Async<T> =
  | { state: "loading" }
  | { state: "error"; message: string }
  | { state: "ready"; data: T };

/** 加载数据，返回状态和重新加载的函数。组件卸载后的结果直接丢弃。 */
export function useAsync<T>(load: () => Promise<T>, deps: unknown[]): [Async<T>, () => void] {
  const [result, setResult] = useState<Async<T>>({ state: "loading" });
  const [version, setVersion] = useState(0);
  useEffect(() => {
    let alive = true;
    load().then(
      (data) => alive && setResult({ state: "ready", data }),
      (error: unknown) =>
        alive &&
        setResult({ state: "error", message: error instanceof Error ? error.message : "加载失败" }),
    );
    return () => {
      alive = false;
    };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [...deps, version]);
  const reload = useCallback(() => setVersion((v) => v + 1), []);
  return [result, reload];
}
