// 搜索命中的页码与阅读器统一使用零基页码；链接不会修改阅读状态。
const MAX_PAGE = 100_000;

export function pageFromSearch(search: string): number | null {
  const raw = new URLSearchParams(search).get("page");
  if (raw === null || !/^\d+$/.test(raw)) return null;
  const page = Number(raw);
  return Number.isSafeInteger(page) && page <= MAX_PAGE ? page : null;
}

export function paperPageLink(paperId: string, page: number | null): string {
  const path = `/papers/${encodeURIComponent(paperId)}`;
  if (page === null || !Number.isSafeInteger(page) || page < 0 || page > MAX_PAGE) return path;
  return `${path}?page=${page}`;
}
