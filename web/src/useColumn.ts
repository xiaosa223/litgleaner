// 阅读页的目录和侧栏：可以折叠，状态按读者保存（第 12.8 节“正文优先”）。
// 停靠在两侧时记住读者的选择；窄屏变成浮层、或双语对照要让出宽度时，只在本次临时展开，默认收起。
import { useEffect, useState } from "react";
import { usePref } from "./prefs";

export function useColumn(key: string, options: { docked: boolean; autoOpen: boolean; yielding: boolean }): {
  open: boolean; toggle: () => void; show: () => void;
} {
  const { docked, autoOpen, yielding } = options;
  const [saved, setSaved] = usePref<boolean | null>(key, null);
  const [temporary, setTemporary] = useState<boolean | null>(null);
  const transient = !docked || yielding;
  useEffect(() => {
    setTemporary(null);
  }, [transient]);
  const open = transient ? temporary ?? false : saved ?? autoOpen;
  const set = (next: boolean) => (transient ? setTemporary(next) : setSaved(next));
  return { open, toggle: () => set(!open), show: () => set(true) };
}
