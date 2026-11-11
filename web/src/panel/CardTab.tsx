// 读后卡片：模型提取的内容按“模型概括 / 模型推断 / 作者原话”区分显示。
import type { PaperView } from "../types";
import { AuthorQuote, ModelText } from "../ui";

interface Limitation {
  text: string;
  source: "author" | "inferred";
  quote?: string | null;
  verified?: boolean;
}

const FIELDS: [string, string, string][] = [
  ["summary", "一句话概括", "用一句话说清这篇做了什么"],
  ["innovation", "创新点", "和已有方法相比，新在哪里"],
  ["conclusion", "主要结论", "作者用什么证据得出了什么结论"],
];

export function CardTab({ paper, onMarkRead }: { paper: PaperView; onMarkRead: () => void }) {
  const data = paper.extraction ?? {};
  const limitations = (data.limitations as Limitation[] | undefined) ?? [];
  const fields = FIELDS.filter(([key]) => typeof data[key] === "string" && data[key]);
  return (
    <>
      <div className="cardhead">
        <b>读后卡片</b>
        <button className="ghost" style={{ marginLeft: "auto" }} onClick={onMarkRead} disabled={paper.reading?.status === "read"}>
          {paper.reading?.status === "read" ? "已读" : "标记为已读"}
        </button>
      </div>
      {!paper.extraction && <div className="empty-note" style={{ marginTop: 12 }}>还没有从全文里提取出要点（只读了摘要，或全文还在处理）。</div>}
      {fields.map(([key, title, prompt], n) => (
        <div className="step done" key={key}>
          <h4><span className="num">{n + 1}</span>{title}</h4>
          <div className="prompt">{prompt}</div>
          <div className="lbl"><span className="badge b-model">AI</span>AI 的分析</div>
          <ModelText text={data[key] as string} />
        </div>
      ))}
      {limitations.length > 0 && (
        <div className="step done">
          <h4><span className="num">{fields.length + 1}</span>不足</h4>
          <div className="prompt">作者承认的局限，和模型推断的问题分开看</div>
          {limitations.map((item, i) => (
            <div key={i}>
              <ModelText text={item.text} kind={item.source === "inferred" ? "inferred" : "model"} />
              {item.quote && item.verified && <AuthorQuote quote={item.quote} page={null} />}
              {item.source === "author" && !item.verified && <div className="tag">引用未能在原文中核实，不作为作者原话显示。</div>}
            </div>
          ))}
        </div>
      )}
    </>
  );
}
