// 文本渲染：行内和独立公式用 KaTeX，表格只保留表格标签，用户高亮按原文片段标出。
// 论文内容是不可信数据：除 KaTeX 的输出外一律按纯文本渲染（KaTeX 默认 trust: false，不生成链接和脚本）。
import katex from "katex";
import "katex/dist/katex.min.css";
import type { ReactNode } from "react";

export function tex(source: string, display: boolean): ReactNode {
  try {
    const html = katex.renderToString(source, { throwOnError: false, displayMode: display, trust: false });
    return <span dangerouslySetInnerHTML={{ __html: html }} />;
  } catch {
    return <code>{source}</code>;
  }
}

// 只合并空白，不去掉首尾空格：公式两侧的空格要保留
const squash = (s: string) => s.replace(/\s+/g, " ");
const needle = (s: string) => squash(s).trim();

/** 把文本按高亮片段切开。片段按空白归一后匹配，找不到的忽略。 */
function marked(text: string, marks: { quote: string; cls: string }[], keyBase: string): ReactNode[] {
  const flat = squash(text);
  const hits = marks
    .map((m) => ({ ...m, at: needle(m.quote) ? flat.indexOf(needle(m.quote)) : -1, len: needle(m.quote).length }))
    .filter((m) => m.at >= 0 && m.len > 0)
    .sort((a, b) => a.at - b.at);
  const out: ReactNode[] = [];
  let pos = 0;
  hits.forEach((hit, n) => {
    if (hit.at < pos) return; // 重叠的只标第一个
    if (hit.at > pos) out.push(flat.slice(pos, hit.at));
    out.push(<mark key={`${keyBase}-m${n}`} className={hit.cls}>{flat.slice(hit.at, hit.at + hit.len)}</mark>);
    pos = hit.at + hit.len;
  });
  if (pos < flat.length) out.push(flat.slice(pos));
  return out;
}

/** 正文：$...$ 和 $$...$$ 渲染为公式，其余为纯文本并标出高亮。 */
export function rich(text: string, marks: { quote: string; cls: string }[] = []): ReactNode[] {
  const clean = text.replace(/<sup>(.*?)<\/sup>/g, "$1").replace(/<[^>]+>/g, "");
  return clean.split(/(\$\$[\s\S]+?\$\$|\$[^$]+\$)/).map((part, i) => {
    if (part.startsWith("$$") && part.length > 4) return <span key={i}>{tex(part.slice(2, -2), true)}</span>;
    if (part.startsWith("$") && part.length > 2) return <span key={i}>{tex(part.slice(1, -1), false)}</span>;
    return <span key={i}>{marked(part, marks, String(i))}</span>;
  });
}

const TABLE_TAGS = new Set(["TABLE", "THEAD", "TBODY", "TR", "TD", "TH"]);

/** 解析器给的表格 HTML 只保留表格结构和合并单元格属性。 */
export function safeTable(html: string): ReactNode {
  const doc = new DOMParser().parseFromString(html, "text/html");
  const build = (node: Node, key: string): ReactNode => {
    if (node.nodeType === Node.TEXT_NODE) {
      // table、tr 等元素里直接出现的空白文本在 DOM 里不合法，丢掉
      const parent = node.parentElement?.tagName ?? "";
      if (["TABLE", "THEAD", "TBODY", "TR"].includes(parent)) return null;
      return <span key={key}>{rich(node.textContent ?? "")}</span>;
    }
    if (node.nodeType !== Node.ELEMENT_NODE) return null;
    const element = node as Element;
    const children = [...element.childNodes].map((child, i) => build(child, `${key}-${i}`));
    if (!TABLE_TAGS.has(element.tagName)) return <span key={key}>{children}</span>;
    const span = (name: string) => {
      const value = parseInt(element.getAttribute(name) ?? "", 10);
      return Number.isFinite(value) && value > 0 && value < 100 ? value : undefined;
    };
    const Tag = element.tagName.toLowerCase() as "table";
    return (
      <Tag key={key} {...({ rowSpan: span("rowspan"), colSpan: span("colspan") } as object)}>
        {children}
      </Tag>
    );
  };
  return [...doc.body.childNodes].map((node, i) => build(node, String(i)));
}

/** 章节编号推断标题层级：3 → h2，3.2 → h3，3.2.1 → h4。 */
export function headingLevel(text: string): 2 | 3 | 4 {
  const match = /^(\d+(?:\.\d+)*)\s/.exec(text.trim());
  if (!match) return 2;
  return Math.min(match[1].split(".").length + 1, 4) as 2 | 3 | 4;
}

export const isReferencesHeading = (text: string) =>
  /^(references|bibliography|参\s*考\s*文\s*献)$/i.test(text.trim());
