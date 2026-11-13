// 全文 PDF 已到、段落结构还没识别出来时阅读页的提示（纯函数，可单测）。
// 段落结构决定原版视图上能不能逐段翻译、提问；识别通常几秒到一分钟。
export const PARSE_POLL_MS = 4000;
export const PARSE_POLL_LIMIT = 75; // 约 5 分钟后不再自动检查，用户仍可刷新

type PaperParse = { has_pdf: boolean; has_document: boolean; fulltext_status: string | null };

export function parseNote(paper: PaperParse, timedOut = false): { text: string; waiting: boolean } | null {
  if (!paper.has_pdf || paper.has_document) return null;
  if (paper.fulltext_status === "failed") {
    return { text: "没能识别这篇 PDF 的段落结构。阅读、选字、高亮和笔记照常可用；逐段翻译和提问需要段落结构。", waiting: false };
  }
  if (paper.fulltext_status !== "available") return null;
  if (timedOut) {
    return { text: "段落结构识别得比平时慢。可以先照常阅读和做笔记，稍后刷新页面再看。", waiting: false };
  }
  return { text: "正在识别段落结构，通常不到一分钟。识别完成后，段落旁会出现“译 / 问 / 记”按钮。", waiting: true };
}

/** 轮询拿到的新状态和页面上的不同，才需要刷新阅读页。 */
export function parseChanged(shown: PaperParse, latest: PaperParse): boolean {
  return shown.has_document !== latest.has_document || shown.fulltext_status !== latest.fulltext_status;
}
