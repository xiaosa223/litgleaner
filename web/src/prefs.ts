// 每个读者自己的显示偏好，存在浏览器里；读不到时用默认值（无痕模式等）。
import { useCallback, useState } from "react";

const PREFIX = "litgleaner-reader-";

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

export function usePref<T>(key: string, fallback: T): [T, (value: T) => void] {
  const [value, setValue] = useState<T>(() => read(key, fallback));
  const update = useCallback(
    (next: T) => {
      setValue(next);
      write(key, next);
    },
    [key],
  );
  return [value, update];
}
