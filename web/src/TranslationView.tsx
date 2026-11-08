// 原文和译文按句渲染：悬停任一句，两边对应的句子同时高亮（设计研究第二轮反馈）。
import type { ReactNode } from "react";
import { rich } from "./text";
import type { Translations } from "./translations";
import type { BlockTranslation } from "./types";

export interface Pairing {
  hover: string | null;
  onHover: (key: string | null) => void;
}

function Sentence({ k, pairing, children }: { k: string; pairing: Pairing; children: ReactNode }) {
  return (
    <span className={`s${pairing.hover === k ? " pair" : ""}`} data-k={k}
      onMouseEnter={() => pairing.onHover(k)} onMouseLeave={() => pairing.onHover(null)}>
      {children}
    </span>
  );
}

/** 原文：有对齐的译文时按句切开，便于配对高亮；否则整段显示。 */
export function EnText({ index, text, tr, marks, pairing }: {
  index: number; text: string; tr: BlockTranslation | undefined; marks: { quote: string; cls: string }[]; pairing: Pairing;
}) {
  if (!tr || tr.sources.length !== tr.sentences.length) return <>{rich(text, marks)}</>;
  return (
    <>
      {tr.sources.map((s, k) => (
        <span key={k}>
          <Sentence k={`${index}:${k}`} pairing={pairing}>{rich(s, marks)}</Sentence>{" "}
        </span>
      ))}
    </>
  );
}

function Status({ tr, onRetry, quiet }: { tr: BlockTranslation; onRetry: () => void; quiet: boolean }) {
  if (!tr.verified && tr.sentences.length === 0) {
    return (
      <span className="tag" style={{ color: "var(--warn)" }}>
        译文没有通过核验，保留原文：{tr.problems.join("；")}。<button className="linkbtn" onClick={onRetry}>重试</button>
      </span>
    );
  }
  if (!tr.verified) {
    return (
      <span className="tag" style={{ color: "var(--warn)" }}>
        未通过核验：{tr.problems.join("；")}。这次译文没有缓存。<button className="linkbtn" onClick={onRetry}>重试</button>
      </span>
    );
  }
  if (tr.stale) {
    return <span className="tag">旧版本译文（术语表或模型已更新）· <button className="linkbtn" onClick={onRetry}>重新翻译</button></span>;
  }
  return quiet ? null : <span className="tag">按课题术语表翻译 · 已缓存</span>;
}

/** 某一段的译文区域：已有译文、正在生成、出错或还没翻译。 */
export function ZhText({ index, translations, pairing, idle, quiet = false }: {
  index: number; translations: Translations; pairing: Pairing; idle: ReactNode; quiet?: boolean; // quiet：正常状态不显示说明
}) {
  const tr = translations.done.get(index);
  const live = translations.live.get(index);
  const error = translations.errors.get(index);
  const retry = () => translations.translate(index);
  if (live !== undefined) {
    const waiting = translations.thinking ? "模型思考中…" : "正在翻译…";
    return <>{live || waiting}<span className="tag">正在翻译 · 按课题术语表统一译名</span></>;
  }
  if (error) {
    return <span className="tag" style={{ color: "var(--warn)" }}>{error} <button className="linkbtn" onClick={retry}>重试</button></span>;
  }
  if (!tr) return <>{idle}</>;
  return (
    <>
      {tr.sentences.map((s, k) => <Sentence key={k} k={`${index}:${k}`} pairing={pairing}>{rich(s)}</Sentence>)}
      <Status tr={tr} onRetry={retry} quiet={quiet} />
    </>
  );
}
