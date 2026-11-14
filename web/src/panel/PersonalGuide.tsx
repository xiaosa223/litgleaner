// 个性化导读：按你的知识库讲解关键概念，给三个带着读的问题（第 12.2、12.7 节）。
// 生成要调用模型，先说明费用再由读者点击；知识库变了只提示，不自动重新生成。
import { useState } from "react";
import { api } from "../api";
import { costHint, KIND_HINT, KIND_LABEL, TREATMENT_LABEL, type GuideConcept, type Quote, type Sourced } from "../guidance";
import type { useGuide } from "../useGuide";
import { AuthorQuote, ModelText, UserText, useToast } from "../ui";

type GuideState = ReturnType<typeof useGuide>;
export type JumpQuote = (quote: Quote) => void;

export function SourcedText({ item, onJump }: { item: Sourced; onJump: JumpQuote }) {
  return (
    <>
      <ModelText text={item.text} kind={item.inference ? "inferred" : "model"} />
      {item.citations.map((quote, n) => (
        <AuthorQuote key={n} quote={quote.quote} page={quote.page} label="原文 · 已核验" onJump={() => onJump(quote)} />
      ))}
    </>
  );
}

function ConceptItem({ concept, onJump, first }: { concept: GuideConcept; onJump: JumpQuote; first: boolean }) {
  const [open, setOpen] = useState(first && concept.treatment === "full");
  return (
    <li className={`gconcept t-${concept.treatment}`}>
      <button className="ghead" aria-expanded={open} onClick={() => setOpen(!open)}>
        <span className="gname">{concept.name}</span>
        <span className={`pill tr-${concept.treatment}`}>{TREATMENT_LABEL[concept.treatment]}</span>
        <span className="chev" aria-hidden>{open ? "−" : "+"}</span>
      </button>
      {open && (
        <div className="gbody">
          {concept.anchor && <><div className="lbl">你之前写过</div><UserText text={concept.anchor} /></>}
          <SourcedText item={concept.explanation} onJump={onJump} />
          {concept.analogy && (
            <div className="analogy">
              <div className="lbl" style={{ marginTop: 0 }}>
                类比{concept.analogy.concept_id ? "你已掌握的" : ""}「{concept.analogy.name}」
              </div>
              <div>{concept.analogy.text}</div>
              <div className="breaks"><b>类比在这里失效：</b>{concept.analogy.breaks}</div>
            </div>
          )}
        </div>
      )}
    </li>
  );
}

/** 生成前先标出已经懂的概念：记为你自己定的“能解释”，导读就少讲这些；之后可以在知识库里改。 */
function Familiar({ state }: { state: GuideState }) {
  const toast = useToast();
  const [saving, setSaving] = useState<string | null>(null);
  const concepts = state.page?.view.plan?.concepts ?? [];
  if (concepts.length === 0) return null;
  const toggle = async (id: string, known: boolean) => {
    setSaving(id);
    try {
      await api.setMastery(id, known ? 1 : 2);
      state.reload();
    } catch (cause) {
      toast(cause instanceof Error ? cause.message : "没能保存，请重试");
    } finally {
      setSaving(null);
    }
  };
  return (
    <div className="familiar">
      <div className="tag">这篇的关键概念里，你已经懂的点一下，导读会少讲：</div>
      <div className="chips" role="group" aria-label="已经懂的概念">
        {concepts.map((c) => {
          const known = c.level >= 2;
          return (
            <button key={c.concept_id} className="chip" aria-pressed={known} disabled={saving === c.concept_id}
              onClick={() => void toggle(c.concept_id, known)}>
              {known && <span aria-hidden>✓ </span>}{c.name}
            </button>
          );
        })}
      </div>
    </div>
  );
}

function Generate({ state }: { state: GuideState }) {
  const { page, busy, generate } = state;
  const plan = page?.view.plan;
  const job = page?.view.job ?? "none";
  if (job === "queued" || job === "retrying") {
    return (
      <div className="gen-wait" role="status">
        <span className="dots" aria-hidden><i /><i /><i /></span>
        正在对照你的知识库准备导读{job === "retrying" ? "（上次没成功，正在重试）" : ""}…
      </div>
    );
  }
  const stale = page?.view.stale && page.view.guide;
  return (
    <div className={stale ? "gen-stale" : "gen-cta"}>
      {stale
        ? <span>你的知识库更新了，导读可以按你现在的掌握情况重新写。</span>
        : <span>{plan?.knowledge_empty
          ? "按这篇论文的关键概念写一份入门导读，并给你三个带着读的问题。你回答后，这里会越来越懂你。"
          : "对照你已经掌握的概念讲解这篇论文：懂的少讲，不懂的用你懂的来类比。"}</span>}
      {!stale && <Familiar state={state} />}
      <div className="row">
        <button className={stale ? "ghost" : "primary sm"} onClick={generate} disabled={busy}>
          {stale ? "重新生成" : "生成我的导读"}
        </button>
        <span className="tag">{plan?.configured ? costHint(plan.estimated_usd) : "需要先配置导读模型"}</span>
      </div>
      {job === "failed" && <div className="warnbox">上次生成没有成功，可以再试一次。</div>}
      {job === "budget_exceeded" && <div className="warnbox">今天的模型预算用完了，明天再生成，或在设置里调高预算。</div>}
    </div>
  );
}

export function PersonalGuide({ state, onJump }: { state: GuideState; onJump: JumpQuote }) {
  const { page, error } = state;
  if (!page) return error ? <div className="warnbox" role="alert">{error}</div> : <div className="skeleton" aria-busy aria-label="正在加载导读" />;
  if (page.view.unavailable && !page.view.guide) return <div className="gen-stale" role="status">{page.view.unavailable}</div>;
  const guide = page.view.guide;
  return (
    <div className="pguide">
      {error && <div className="warnbox" role="alert">{error}</div>}
      {(!guide || page.view.stale || page.view.job !== "none") && <Generate state={state} />}
      {guide && (
        <>
          <div className="sec"><h4>这篇在做什么</h4><SourcedText item={guide.overview} onJump={onJump} /></div>
          {guide.questions.length > 0 && (
            <div className="sec">
              <h4>带着这三个问题读</h4>
              <ol className="gq">
                {guide.questions.map((q) => (
                  <li key={q.index}>
                    <span className={`qk qk-${q.kind}`} title={KIND_HINT[q.kind]}>{KIND_LABEL[q.kind]}</span>
                    <span>{q.text}{q.section && <span className="tag"> · {q.section}</span>}</span>
                  </li>
                ))}
              </ol>
              <div className="tag">读完到“读后”标签用一到三句话回答，先写再看评价。</div>
            </div>
          )}
          {guide.concepts.length > 0 && (
            <div className="sec">
              <h4>先弄懂这几个概念</h4>
              <ul className="list gconcepts">
                {guide.concepts.map((c, i) => <ConceptItem key={c.concept_id} concept={c} onJump={onJump} first={i === 0} />)}
              </ul>
            </div>
          )}
          {guide.sections.length > 0 && (
            <div className="sec">
              <h4>先读哪里</h4>
              <ul className="list">{guide.sections.map((s) => <li key={s.section}><b>{s.section}</b><div className="tag" style={{ fontSize: 12 }}>{s.why}</div></li>)}</ul>
            </div>
          )}
          <div className="tag">导读由模型 {guide.model} 生成；“原文 · 已核验”的引文都在论文里逐字找到过。</div>
        </>
      )}
    </div>
  );
}
