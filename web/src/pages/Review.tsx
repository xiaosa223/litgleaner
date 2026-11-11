// 复习：先自己回忆，再看当时写的回答，然后自评“记得 / 模糊 / 忘了”。自评不调用模型（第 12.7 节）。
import { useState } from "react";
import type { Concept } from "../types";
import { SourcedQuote, UserText } from "../ui";

type Rating = "记得" | "模糊" | "忘了";
const RATINGS: Rating[] = ["记得", "模糊", "忘了"];

export function Review({ concepts }: { concepts: Concept[] }) {
  const [step, setStep] = useState(0);
  const [draft, setDraft] = useState("");
  const [revealed, setRevealed] = useState(false);
  const [ratings, setRatings] = useState<Rating[]>([]);
  const concept = concepts[step];

  if (!concept) {
    const count = (r: Rating) => ratings.filter((x) => x === r).length;
    return (
      <div className="empty">
        <h2>{concepts.length === 0 ? "今天没有要复习的概念" : "今天的复习做完了"}</h2>
        {ratings.length > 0 && <p>{RATINGS.map((r) => `${r} ${count(r)}`).join(" · ")}。忘了的概念会更早再出现。</p>}
        {ratings.length > 0 && <button className="ghost" onClick={() => { setStep(0); setRatings([]); }}>再来一遍</button>}
      </div>
    );
  }
  const rate = (rating: Rating) => {
    setRatings([...ratings, rating]);
    setStep(step + 1);
    setDraft("");
    setRevealed(false);
  };
  return (
    <div className="review">
      <div className="tag">第 {step + 1} / {concepts.length} 个</div>
      <div className="q">用自己的话说说：{concept.translation}（{concept.name}）是什么？</div>
      <label className="sr-only" htmlFor="recall">先写下你记得的</label>
      <textarea id="recall" className="usernote" value={draft} onChange={(e) => setDraft(e.target.value)}
        placeholder="先写下你记得的，写不出来也没关系" disabled={revealed} />
      {!revealed && <div className="row"><button className="primary sm" onClick={() => setRevealed(true)}>看我当时写的</button></div>}
      {revealed && (
        <>
          <div className="lbl"><span className="badge b-user">你</span>你当时写的</div>
          {concept.answer ? <UserText text={concept.answer} /> : <div className="tag">这个概念你还没有写过回答，下面是论文里的定义。</div>}
          {concept.definition && <><div className="lbl">论文里的定义</div><SourcedQuote {...concept.definition} /></>}
          <div className="lbl">和现在记得的比一比</div>
          <div className="rate" role="group" aria-label="自评">
            {RATINGS.map((r) => <button key={r} className="ghost" onClick={() => rate(r)}>{r}</button>)}
          </div>
        </>
      )}
    </div>
  );
}
