// 复习：先自己回忆，再看当时写的回答，然后自评“记得 / 模糊 / 忘了”。自评不调用模型（第 12.7 节）。
import { useState } from "react";
import { api } from "../api";
import { answerNote, RATING_LABEL, RATINGS, translationOf, type ConceptCard, type Rating } from "../guidance";
import { paperPageLink } from "../pageLink";
import { UserText, useToast } from "../ui";
import { useAsync } from "../useAsync";

function Session({ concepts, onDone }: { concepts: ConceptCard[]; onDone: () => void }) {
  const toast = useToast();
  const [step, setStep] = useState(0);
  const [draft, setDraft] = useState("");
  const [revealed, setRevealed] = useState(false);
  const [saving, setSaving] = useState(false);
  const [ratings, setRatings] = useState<Rating[]>([]);
  const card = concepts[step];

  if (!card) {
    const count = (r: Rating) => ratings.filter((x) => x === r).length;
    return (
      <div className="empty">
        {ratings.length > 0 && <div className="done-mark" aria-hidden>✓</div>}
        <h2>{concepts.length === 0 ? "今天没有要复习的概念" : "今天的复习做完了"}</h2>
        {ratings.length > 0 && count("forgot") === 0 && count("fuzzy") === 0 && <p>全都记得，间隔会继续拉长。</p>}
        {concepts.length === 0 && <p>回答过引导问题、评价为“能解释”以上的概念，会在 1、3、7、14、30 天后提醒你回忆。</p>}
        {ratings.length > 0 && <p>{RATINGS.map((r) => `${RATING_LABEL[r]} ${count(r)}`).join(" · ")}。忘了的概念会更早再出现。</p>}
      </div>
    );
  }
  const { summary } = card;
  const translation = translationOf(summary);
  const rate = async (rating: Rating) => {
    setSaving(true);
    try {
      await api.rateReview(summary.concept.id, rating);
      setRatings([...ratings, rating]);
      setStep(step + 1);
      setDraft("");
      setRevealed(false);
      if (step + 1 >= concepts.length) onDone();
    } catch (cause) {
      toast(cause instanceof Error ? cause.message : "没能记下，请重试");
    } finally {
      setSaving(false);
    }
  };
  return (
    <div className="review">
      <div className="tag">第 {step + 1} / {concepts.length} 个</div>
      <div className="q">用自己的话说说：{translation ? `${translation}（${summary.concept.name}）` : summary.concept.name}是什么？</div>
      <label className="sr-only" htmlFor="recall">先写下你记得的</label>
      <textarea id="recall" className="usernote" value={draft} onChange={(e) => setDraft(e.target.value)}
        placeholder="先写下你记得的，写不出来也没关系" disabled={revealed} />
      {!revealed && <div className="row"><button className="primary sm" onClick={() => setRevealed(true)}>看我当时写的</button></div>}
      {revealed && (
        <>
          <div className="lbl"><span className="badge b-user">你</span>你当时写的</div>
          {card.answers.length > 0
            ? card.answers.slice(0, 2).map((a) => (
              <div key={a.card_id}>
                <UserText text={a.text} />
                {answerNote(a.verdict, a.overall) && <div className={`tag ${a.verdict === "misunderstood" ? "warn-text" : ""}`}>{answerNote(a.verdict, a.overall)}</div>}
              </div>
            ))
            : <div className="tag">这个概念你还没有写过回答，下面是论文里的定义。</div>}
          {summary.definition && (
            <>
              <div className="lbl">论文里的定义</div>
              <blockquote className="author">
                <span className="badge b-author">作者原话</span> “{summary.definition.quote}”
                <a className="pg" href={paperPageLink(summary.definition.paper_id, summary.definition.page)}>第 {summary.definition.page + 1} 页 ↗</a>
              </blockquote>
            </>
          )}
          <div className="lbl">和现在记得的比一比</div>
          <div className="rate" role="group" aria-label="自评">
            {RATINGS.map((r) => <button key={r} className="ghost" disabled={saving} onClick={() => void rate(r)}>{RATING_LABEL[r]}</button>)}
          </div>
        </>
      )}
    </div>
  );
}

export function Review({ onDone }: { onDone: () => void }) {
  const [due] = useAsync(() => api.reviewDue(), []);
  if (due.state === "loading") return <div className="skeleton" aria-busy />;
  if (due.state === "error") return <div className="view-msg">{due.message}</div>;
  return <Session concepts={due.data} onDone={onDone} />;
}
