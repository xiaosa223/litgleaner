// 概念关系与疑似重复（第 12.7 节）：模型在生成导读时提议并附理由，你确认或删除；不影响掌握程度。
import { useState } from "react";
import { api } from "../api";
import { relationLabel, type ConceptCard, type MergeView } from "../guidance";
import { useToast } from "../ui";
import { useAsync } from "../useAsync";

function useDecide(onDone: () => void) {
  const toast = useToast();
  const [busy, setBusy] = useState<string | null>(null);
  const decide = async (target: string, decision: "confirmed" | "rejected") => {
    setBusy(target);
    try {
      await api.decideConcept(target, decision);
      onDone();
    } catch (cause) {
      toast(cause instanceof Error ? cause.message : "没能保存，请重试");
    } finally {
      setBusy(null);
    }
  };
  return { busy, decide };
}

export function Related({ card, onSelect, onChanged }: {
  card: ConceptCard; onSelect: (conceptId: string) => void; onChanged: () => void;
}) {
  const { busy, decide } = useDecide(onChanged);
  if (card.related.length === 0 && card.merged.length === 0) return null;
  return (
    <>
      {card.merged.length > 0 && (
        <div className="tag" style={{ marginTop: 6 }}>
          也叫：{card.merged.map((m) => (
            <span key={m.merge_id} className="alias">
              {m.name}
              <button className="link-btn" disabled={busy === m.merge_id} title="不是同一个概念，拆开"
                onClick={() => void decide(m.merge_id, "rejected")}>拆开</button>
            </span>
          ))}
        </div>
      )}
      {card.related.length > 0 && (
        <>
          <div className="lbl">相关概念</div>
          <ul className="related">
            {card.related.map((item) => (
              <li key={item.link_id} className={item.confirmed ? "" : "proposed"}>
                <span className="rk">{relationLabel(item)}</span>
                <button className="link-btn" onClick={() => onSelect(item.concept_id)}>{item.name}</button>
                {!item.confirmed && <span className="badge b-model">模型提议</span>}
                <div className="tag">{item.reason}</div>
                {!item.confirmed && (
                  <div className="row" style={{ marginTop: 2 }}>
                    <button className="ghost" disabled={busy === item.link_id} onClick={() => void decide(item.link_id, "confirmed")}>对</button>
                    <button className="ghost" disabled={busy === item.link_id} onClick={() => void decide(item.link_id, "rejected")}>删除</button>
                  </div>
                )}
              </li>
            ))}
          </ul>
        </>
      )}
    </>
  );
}

export function MergeSuggestions({ version, onChanged }: { version: number; onChanged: () => void }) {
  const [loaded, reload] = useAsync(() => api.pendingMerges(), [version]);
  const { busy, decide } = useDecide(() => { reload(); onChanged(); });
  if (loaded.state !== "ready" || loaded.data.length === 0) return null;
  return (
    <div className="merge-box" role="region" aria-label="疑似同一概念">
      <b>这几组可能是同一个概念</b>
      {loaded.data.map((m: MergeView) => (
        <div className="merge-row" key={m.merge_id}>
          <div>“{m.from_name}” 和 “{m.into_name}”<div className="tag">{m.reason}</div></div>
          <div className="row" style={{ marginTop: 0 }}>
            <button className="primary sm" disabled={busy === m.merge_id} onClick={() => void decide(m.merge_id, "confirmed")}>合并</button>
            <button className="ghost" disabled={busy === m.merge_id} onClick={() => void decide(m.merge_id, "rejected")}>不是</button>
          </div>
        </div>
      ))}
      <div className="tag">合并后按一个概念记掌握程度和回答；原来的记录都保留。</div>
    </div>
  );
}
