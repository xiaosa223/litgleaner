// 读后卡片：模型提取的内容按“模型概括 / 模型推断 / 作者原话”区分显示。
// 先写后看、比较异同、导出提纲在路线图 5f 实现，这里先展示已有的提取结果。
import type { AssessmentEntry, PaperView } from "../types";
import { AuthorQuote, ModelText, SOON } from "../ui";

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

function ScoreLine({ entry }: { entry: AssessmentEntry }) {
  const notes = [
    entry.provisional && "只读了摘要，临时分",
    entry.undetermined.length > 0 && `无法判断：${entry.undetermined.join("、")}`,
    entry.stale && "旧标准下的结果",
  ].filter(Boolean);
  return (
    <div className="row" style={{ marginTop: 0 }}>
      <span className="score" style={{ fontSize: 16 }}>{entry.score}</span>
      <span className="lvl">{entry.tier_label}</span>
      <span className="tag">{entry.topic_name}{notes.length ? ` · ${notes.join(" · ")}` : ""}</span>
    </div>
  );
}

export function CardTab({ paper, onMarkRead }: { paper: PaperView; onMarkRead: () => void }) {
  const data = paper.extraction ?? {};
  const limitations = (data.limitations as Limitation[] | undefined) ?? [];
  const fields = FIELDS.filter(([key]) => typeof data[key] === "string" && data[key]);
  return (
    <>
      <div className="cardhead">
        <b>读后卡片</b>
        <span className="tag">{SOON.card}</span>
        <button className="primary sm" disabled title={SOON.export}>导出组会提纲</button>
        <button className="ghost" onClick={onMarkRead} disabled={paper.reading?.status === "read"}>
          {paper.reading?.status === "read" ? "已读" : "标记为已读"}
        </button>
      </div>
      {Object.entries(paper.assessments).map(([topic, entry]) => <ScoreLine key={topic} entry={entry} />)}
      {!paper.extraction && <div className="empty-note" style={{ marginTop: 12 }}>还没有全文提取结果（只读了摘要，或全文还在处理）。</div>}
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
