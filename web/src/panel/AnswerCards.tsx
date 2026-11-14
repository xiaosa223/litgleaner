// 回答卡片：先写后看。用户写一到三句话，提交后才显示评价和参考答案（第 12.7 节）。
import { useState } from "react";
import { api } from "../api";
import {
  costHint, KIND_HINT, KIND_LABEL, lengthHint, OVERALL_LABEL, provenLevel, VERDICT_LABEL, waiting,
  type CardView, type GuideQuestion,
} from "../guidance";
import type { useGuide } from "../useGuide";
import { AuthorQuote, UserText, useToast } from "../ui";
import { SourcedText, type JumpQuote } from "./PersonalGuide";

type GuideState = ReturnType<typeof useGuide>;

const LEVEL_NAME = { 2: "能解释", 3: "能应用" } as const;

function Evaluation({ view, kind, names, paperId, onJump }: {
  view: CardView; kind: GuideQuestion["kind"]; names: Record<string, string>; paperId: string; onJump: JumpQuote;
}) {
  const toast = useToast();
  const [flagged, setFlagged] = useState(false);
  const evaluation = view.evaluation!;
  const flag = async () => {
    try {
      await api.markEvaluationWrong(paperId, view.card!.id, "");
      setFlagged(true);
      toast("已记下，会用来改进评价；你也可以在知识库里直接改掌握程度。");
    } catch (cause) {
      toast(cause instanceof Error ? cause.message : "没能记下，请重试");
    }
  };
  return (
    <div className={`evaluation ov-${evaluation.overall}`}>
      <div className="evhead">
        <b>{OVERALL_LABEL[evaluation.overall]}</b>
        <button className="flag-btn" onClick={() => void flag()} disabled={flagged}>{flagged ? "已标为有误" : "评价有误？"}</button>
      </div>
      {evaluation.verdicts.map((verdict) => (
        <div className="verdict" key={verdict.concept_id}>
          <span className={`vk vk-${verdict.verdict}`}>{VERDICT_LABEL[verdict.verdict]}</span>
          <span className="vname">{names[verdict.concept_id] ?? ""}</span>
          {(() => {
            const level = provenLevel(kind, verdict.verdict, evaluation.overall);
            return level >= 2 && <span className="levelup">记进知识库 · {LEVEL_NAME[level as 2 | 3]}</span>;
          })()}
          <div>{verdict.comment}</div>
          {verdict.citations.map((quote, n) => (
            <AuthorQuote key={n} quote={quote.quote} page={quote.page} label="原文 · 已核验" onJump={() => onJump(quote)} />
          ))}
        </div>
      ))}
      <details className="reference">
        <summary>参考答案</summary>
        <SourcedText item={evaluation.reference} onJump={onJump} />
      </details>
      {evaluation.advice && <div className="advice">下一步：{evaluation.advice}</div>}
    </div>
  );
}

function Card({ question, view, state, names, paperId, onJump }: {
  question: GuideQuestion; view: CardView; state: GuideState; names: Record<string, string>; paperId: string; onJump: JumpQuote;
}) {
  const [editing, setEditing] = useState(!view.card);
  const [draft, setDraft] = useState(view.card?.text ?? "");
  const page = state.page!;
  const hint = lengthHint(draft);
  const submit = async (evaluate: boolean) => {
    if (await state.answer(question.index, draft.trim(), evaluate)) setEditing(false);
  };
  return (
    <div className={`step${view.evaluation ? " done" : ""}`}>
      <h4><span className="num">{question.index + 1}</span>{KIND_LABEL[question.kind]}题</h4>
      <div className="prompt">{KIND_HINT[question.kind]}</div>
      <div className="q">{question.text}</div>
      {editing ? (
        <>
          <label className="sr-only" htmlFor={`ans-${question.index}`}>你的回答</label>
          <textarea id={`ans-${question.index}`} className="usernote" value={draft} onChange={(e) => setDraft(e.target.value)}
            placeholder="用自己的话写一到三句，不用查书，写不出来也是收获" maxLength={2000} />
          {hint && <div className="tag">{hint}</div>}
          <div className="row">
            <button className="primary sm" disabled={!draft.trim() || state.busy} onClick={() => void submit(page.evaluate_configured)}>
              {page.evaluate_configured ? "提交，看评价" : "提交"}
            </button>
            {page.evaluate_configured && <button className="ghost" disabled={!draft.trim() || state.busy} onClick={() => void submit(false)}>先存着</button>}
            {view.card && <button className="ghost" onClick={() => { setEditing(false); setDraft(view.card!.text); }}>取消</button>}
            <span className="tag">{page.evaluate_configured ? `评价${costHint(page.evaluate_estimated_usd)}` : "配置“回答评价”模型后可以看评价"}</span>
          </div>
        </>
      ) : view.card && (
        <>
          <UserText text={view.card.text} />
          <div className="row" style={{ marginTop: 4 }}>
            <button className="ghost" onClick={() => setEditing(true)} disabled={waiting(view.evaluation_state)}
              title={waiting(view.evaluation_state) ? "评价完成后再修改" : undefined}>修改</button>
            {view.revisions > 1 && <span className="tag">改过 {view.revisions - 1} 次，旧版本都保留着</span>}
            {!view.evaluation && view.evaluation_state === "none" && (
              <button className="ghost" disabled={state.busy} onClick={() => state.evaluate(view.card!.id)}>看评价</button>
            )}
          </div>
        </>
      )}
      {waiting(view.evaluation_state) && (
        <div className="gen-wait" role="status"><span className="dots" aria-hidden><i /><i /><i /></span>正在对照原文看你的回答…</div>
      )}
      {view.evaluation_state === "failed" && <div className="warnbox">评价没有成功，可以点“看评价”再试。</div>}
      {view.evaluation_state === "budget_exceeded" && <div className="warnbox">今天的模型预算用完了，回答已保存，明天再看评价。</div>}
      {view.evaluation && !editing && <Evaluation view={view} kind={question.kind} names={names} paperId={paperId} onJump={onJump} />}
    </div>
  );
}

export function AnswerCards({ state, paperId, onJump, onOpenGuide }: {
  state: GuideState; paperId: string; onJump: JumpQuote; onOpenGuide: () => void;
}) {
  const guide = state.page?.view.guide;
  if (!guide) {
    return (
      <div className="step">
        <h4>回答引导问题</h4>
        <div className="prompt">先在“导读”里生成导读，会得到三个带着读的问题。读完用自己的话回答，懂了的概念会记进你的知识库。</div>
        <button className="ghost" onClick={onOpenGuide}>去导读</button>
      </div>
    );
  }
  const names = Object.fromEntries(guide.concepts.map((c) => [c.concept_id, c.name]));
  const error = state.error && <div className="warnbox" role="alert">{state.error}</div>;
  const answered = state.page!.cards.filter((c) => c.card).length;
  return (
    <>
      <div className="cardhead">
        <b>回答引导问题</b>
        <span className="tag">{answered} / 3 · 每题可以跳过</span>
        <div className="meter"><i style={{ width: `${(answered / 3) * 100}%` }} /></div>
      </div>
      {error}
      {guide.questions.map((question) => (
        <Card key={`${guide.id}-${question.index}`} question={question} view={state.page!.cards[question.index]}
          state={state} names={names} paperId={paperId} onJump={onJump} />
      ))}
    </>
  );
}
