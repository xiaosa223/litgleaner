// 阅读位置：滚动停下 1.5 秒后保存当前页；第一次滚动会把“未开始 / 待读”改成“在读”。
import { useCallback, useRef } from "react";
import { api } from "./api";
import type { PaperView, ReadingStatus } from "./types";

const SAVE_DELAY_MS = 1500;

export function useReadingPosition(paper: PaperView | null, view: string, onStatusChange: () => void) {
  const timer = useRef<number | undefined>(undefined);
  const last = useRef<number | null>(null);
  return useCallback(
    (page: number) => {
      if (!paper || page === last.current) return;
      last.current = page;
      window.clearTimeout(timer.current);
      timer.current = window.setTimeout(() => {
        const current = paper.reading?.status;
        const status: ReadingStatus = current && current !== "queued" ? current : "reading";
        // 保存失败不打断阅读，下次翻页再试
        api.setReading(paper.paper_id, status, { view, page }).then(
          () => status !== current && onStatusChange(),
          () => { last.current = null; },
        );
      }, SAVE_DELAY_MS);
    },
    [paper, view, onStatusChange],
  );
}

/** 当前页：视口上方三分之一处所在的页面或段落。 */
export function currentPage(scroller: HTMLElement, selector: string, attribute: string): number | null {
  const line = scroller.getBoundingClientRect().top + scroller.clientHeight / 3;
  let page: number | null = null;
  for (const element of scroller.querySelectorAll<HTMLElement>(selector)) {
    if (element.getBoundingClientRect().top > line) break;
    const value = element.dataset[attribute];
    if (value !== undefined) page = Number(value);
  }
  return page;
}
