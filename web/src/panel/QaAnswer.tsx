import { paperPageLink } from "../pageLink";
import { qaStatementText, type QaCitation, type QaView } from "../qa";
import { AuthorQuote, ModelText } from "../ui";

export function QaAnswer({ answer, paperId, onJump }: {
  answer: NonNullable<QaView["answer"]>; paperId: string; onJump: (citation: QaCitation) => void;
}) {
  const papers = answer.context?.papers;
  return (
    <div className="msg bot qa-answer">
      {answer.statements.map((statement, index) => (
        <div className="qa-statement" key={index}>
          <ModelText text={qaStatementText(statement.text, papers)} kind={statement.inference ? "inferred" : "model"} />
          {statement.citations.map((citation, n) => (
            <div key={n}>
              <AuthorQuote quote={citation.quote} page={citation.page} label={citation.paper_id === paperId ? "本篇原文 · 已核验" : "相关论文原文 · 已核验"} onJump={() => onJump(citation)} />
              {citation.paper_id !== paperId && <a href={paperPageLink(citation.paper_id, citation.page)}>《{papers?.[citation.paper_id] || citation.paper_id}》 ↗</a>}
            </div>
          ))}
          {statement.note_citations.map((citation, n) => (
            <blockquote className="qa-user-source" key={n}>
              <span className="badge b-user">{citation.kind === "question" ? "你的历史问题" : "你的笔记"}</span> “{citation.quote}”
              {citation.paper_id !== paperId && <a href={paperPageLink(citation.paper_id, null)}>《{papers?.[citation.paper_id] || citation.paper_id}》 ↗</a>}
            </blockquote>
          ))}
        </div>
      ))}
      <div className="tag">模型：{answer.model}{answer.statements.some((item) => item.citations.length) ? " · 引文已由服务端核验" : " · 未获得可核验的论文原文引用"}</div>
    </div>
  );
}
