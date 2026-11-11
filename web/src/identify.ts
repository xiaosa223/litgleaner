// 认出用户贴进来的是 DOI、arXiv 编号还是链接。只做格式识别，元数据由后端向学术数据源查（R16）。
export type Identified =
  | { kind: "doi"; value: string }
  | { kind: "arxiv"; value: string }
  | { kind: "url"; value: string };

const DOI = /\b(10\.\d{4,9}\/[^\s"<>]+)/i;
const ARXIV_URL = /arxiv\.org\/(?:abs|pdf)\/(\d{4}\.\d{4,5}(?:v\d+)?)/i;
const ARXIV_ID = /^(?:arxiv:\s*)?(\d{4}\.\d{4,5}(?:v\d+)?)$/i;

export function identify(input: string): Identified | null {
  const text = input.trim();
  if (!text) return null;
  const doi = DOI.exec(text);
  if (doi) return { kind: "doi", value: doi[1].replace(/[.,;:)\]]+$/, "") };
  const arxiv = ARXIV_URL.exec(text) ?? ARXIV_ID.exec(text);
  if (arxiv) return { kind: "arxiv", value: arxiv[1] };
  if (/^https?:\/\/\S+$/i.test(text)) return { kind: "url", value: text };
  return null;
}
