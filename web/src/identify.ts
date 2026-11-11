// 认出用户贴进来的是 DOI、arXiv 编号还是链接。只做格式识别，元数据由后端向学术数据源查（R16）。
export type Identified =
  | { kind: "doi"; value: string }
  | { kind: "arxiv"; value: string }
  | { kind: "url"; value: string };

const DOI = /\b(10\.\d{4,9}\/[^\s"<>]+)/i;
const ARXIV_URL = /arxiv\.org\/(?:abs|pdf)\/(\d{4}\.\d{4,5}(?:v\d+)?)/i;
const ARXIV_ID = /^(?:arxiv:\s*)?(\d{4}\.\d{4,5}(?:v\d+)?)$/i;

function cleanDoi(raw: string): string {
  let value = raw.replace(/[.,;:>]+$/, "");
  for (const [opening, closing] of [["(", ")"], ["[", "]"], ["{", "}"]]) {
    while (value.endsWith(closing) && value.split(closing).length > value.split(opening).length) value = value.slice(0, -1);
  }
  return value;
}

export function identify(input: string): Identified | null {
  const text = input.trim();
  if (!text) return null;
  const doi = DOI.exec(text);
  if (doi) return { kind: "doi", value: cleanDoi(doi[1]) };
  const arxiv = ARXIV_URL.exec(text) ?? ARXIV_ID.exec(text);
  if (arxiv) return { kind: "arxiv", value: arxiv[1] };
  if (/^https?:\/\/\S+$/i.test(text)) return { kind: "url", value: text };
  return null;
}

const FIRST_PAGE_CHARS = 3000; // 首页前段：标题、作者、摘要、页眉页脚，避开参考文献里的 DOI
const ARXIV_STAMP = /arxiv:\s*(\d{4}\.\d{4,5})(?:v\d+)?/i;

/** 从 PDF 首页文字里找本文的 DOI 或 arXiv 编号（与后端 domain/inbox.py 的规则一致）。 */
export function findIdentifier(firstPage: string): Identified | null {
  const head = firstPage.slice(0, FIRST_PAGE_CHARS);
  const doi = DOI.exec(head);
  if (doi) return { kind: "doi", value: cleanDoi(doi[1]) };
  const stamp = ARXIV_STAMP.exec(head);
  return stamp ? { kind: "arxiv", value: stamp[1] } : null;
}
