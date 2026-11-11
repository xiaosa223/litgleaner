// 每个读者自己的显示偏好，存在浏览器里；读不到时用默认值（无痕模式等）。
// 同一个偏好在多个组件里使用时保持同步（设置页改了，其他页面立即生效）。
import { useCallback, useSyncExternalStore } from "react";

const PREFIX = "litgleaner-reader-";
const cache = new Map<string, unknown>();
const listeners = new Set<() => void>();

function read<T>(key: string, fallback: T): T {
  try {
    const raw = localStorage.getItem(PREFIX + key);
    return raw ? (JSON.parse(raw) as T) : fallback;
  } catch {
    return fallback;
  }
}

function write(key: string, value: unknown): void {
  try {
    localStorage.setItem(PREFIX + key, JSON.stringify(value));
  } catch {
    // 存不了就只在本次会话生效
  }
}

function current<T>(key: string, fallback: T): T {
  if (!cache.has(key)) cache.set(key, read(key, fallback));
  return cache.get(key) as T;
}

function subscribe(listener: () => void): () => void {
  listeners.add(listener);
  return () => listeners.delete(listener);
}

export function usePref<T>(key: string, fallback: T): [T, (value: T) => void] {
  const value = useSyncExternalStore(subscribe, () => current(key, fallback));
  const update = useCallback(
    (next: T) => {
      cache.set(key, next);
      write(key, next);
      listeners.forEach((listener) => listener());
    },
    [key],
  );
  return [value, update];
}

/** 窗口是否满足媒体查询，随窗口变化更新。 */
export function useMedia(query: string): boolean {
  const subscribeMedia = useCallback(
    (listener: () => void) => {
      const media = window.matchMedia(query);
      media.addEventListener("change", listener);
      return () => media.removeEventListener("change", listener);
    },
    [query],
  );
  return useSyncExternalStore(subscribeMedia, () => window.matchMedia(query).matches);
}

/** 翻译的显示方式：原文、全文覆盖（译文盖在原文位置上）、双栏对照。 */
export type TranslateMode = "off" | "overlay" | "column";

/** 翻译方式偏好；旧版本只存了“是否双语对照”，读得到就沿用。 */
export function useTranslateMode(): [TranslateMode, (mode: TranslateMode) => void] {
  const [legacy] = usePref<boolean>("bilingual", false);
  const [mode, setMode] = usePref<TranslateMode | null>("translate-mode", null);
  return [mode ?? (legacy ? "column" : "off"), setMode];
}
