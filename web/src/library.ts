// 文库的合并、筛选和检索：纯函数。数据来自 /api/inbox，同一篇论文可能同时在推荐和在读里。
import type { Inbox, InboxItem, ReadingStatus } from "./types.ts";

export type LibraryStatus = ReadingStatus | "new";
export type StatusFilter = LibraryStatus | "all";

export const statusOf = (item: InboxItem): LibraryStatus => item.reading_status ?? "new";

/** 在读 → 待读 → 其余推荐，按编号去重。 */
export function mergeLibrary(inbox: Inbox): InboxItem[] {
  const seen = new Set<string>();
  return [...inbox.reading, ...inbox.queued, ...inbox.recommended].filter((item) => {
    if (seen.has(item.paper_id)) return false;
    seen.add(item.paper_id);
    return true;
  });
}

export function countByStatus(items: InboxItem[]): Record<StatusFilter, number> {
  const counts: Record<StatusFilter, number> = { all: items.length, new: 0, reading: 0, queued: 0, read: 0, abandoned: 0 };
  for (const item of items) counts[statusOf(item)] += 1;
  return counts;
}

const haystack = (item: InboxItem) => [item.title, item.authors.join(" "), item.venue ?? ""].join(" ").toLowerCase();

/** 每个词都要出现在标题、作者或出处里；空查询返回空。 */
export function searchPapers(items: InboxItem[], query: string): InboxItem[] {
  const words = query.toLowerCase().split(/\s+/).filter(Boolean);
  if (words.length === 0) return [];
  return items.filter((item) => {
    const text = haystack(item);
    return words.every((word) => text.includes(word));
  });
}

export function filterLibrary(
  items: InboxItem[],
  filter: { status: StatusFilter; topicId: string | null; query: string },
): InboxItem[] {
  const matched = filter.query.trim() ? new Set(searchPapers(items, filter.query)) : null;
  return items.filter(
    (item) =>
      (filter.status === "all" || statusOf(item) === filter.status) &&
      (filter.topicId === null || item.topic_id === filter.topicId) &&
      (matched === null || matched.has(item)),
  );
}
