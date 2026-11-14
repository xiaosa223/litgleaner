// 导读：读之前先知道这篇是什么、为什么推荐给你。背景卡、引导问题等讲解接上后也放在这里（第 12.2 节）。
import type { AssessmentEntry, PaperView } from "../types";
import { ModelText } from "../ui";
import type { useGuide } from "../useGuide";
import { MetadataPrompt } from "./Metadata";
import { PersonalGuide, type JumpQuote } from "./PersonalGuide";

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

export function GuideTab({ paper, guide, onJump, onChange }: {
  paper: PaperView; guide: ReturnType<typeof useGuide>; onJump: JumpQuote; onChange: () => void;
}) {
  const summary = paper.extraction?.summary;
  const scores = Object.entries(paper.assessments);
  const authors = paper.authors.slice(0, 8).join("、") + (paper.authors.length > 8 ? " 等" : "");
  return (
    <>
      <div className="sec">
        <h4>这篇论文</h4>
        <div style={{ font: "600 15px/1.45 var(--serif)" }}>{paper.title}</div>
        <div className="tag" style={{ marginTop: 4, fontSize: 12 }}>
          {[authors, paper.venue, paper.published_at].filter(Boolean).join(" · ")}
        </div>
        <MetadataPrompt paper={paper} onSaved={onChange} />
        {paper.doi && (
          <a className="link" style={{ fontSize: 12 }} href={`https://doi.org/${encodeURI(paper.doi)}`} target="_blank" rel="noopener noreferrer">doi:{paper.doi}</a>
        )}
      </div>
      <PersonalGuide state={guide} onJump={onJump} />
      {scores.length > 0 && (
        <div className="sec">
          <h4>阅读价值分</h4>
          {scores.map(([topic, entry]) => <ScoreLine key={topic} entry={entry} />)}
        </div>
      )}
      {typeof summary === "string" && summary && (
        <div className="sec"><h4>一句话</h4><ModelText text={summary} /></div>
      )}
      <div className="sec">
        <h4>摘要</h4>
        {paper.abstract
          ? <p style={{ margin: 0, font: "14px/1.7 var(--serif)" }}>{paper.abstract}</p>
          : <div className="empty-note">没有摘要。</div>}
      </div>
    </>
  );
}
