// 全文覆盖（ARCHITECTURE 12.3）：译文按段落的 bbox 盖在原文位置上，保留版式。
// 只覆盖已有译文的段落，其余仍显示原文；鼠标移到段落上时译文淡出，露出原文。
// 校验失败、没有译文的段落不覆盖（12.3：校验失败的段落保留原文）。
import { useLayoutEffect, useRef } from "react";
import { fitFontSize, overlayBaseSize } from "./fit";
import type { FlowTranslation } from "./FlowView";
import { rich } from "./text";
import type { Block } from "./types";

const MIN_RATIO = 0.55; // 再小就看不清了，宁可让译文往下多占一点
const pct = (v: number) => `${(v / 10).toFixed(2)}%`;

function OverlayText({ block, pageWidth, sentences, warn }: {
  block: Block; pageWidth: number; sentences: string[]; warn: boolean;
}) {
  const box = useRef<HTMLDivElement>(null);
  const [x0, y0, x1, y1] = block.bbox!;
  const text = sentences.join(" ");

  useLayoutEffect(() => {
    const element = box.current;
    if (!element) return;
    element.style.height = pct(y1 - y0);
    const fits = (size: number) => {
      element.style.fontSize = `${size}px`;
      return element.scrollHeight <= element.clientHeight + 1;
    };
    const base = overlayBaseSize(pageWidth);
    const size = fitFontSize(fits, base, base * MIN_RATIO);
    if (!fits(size)) element.style.height = "auto"; // 最小字号也放不下：向下延伸，不截掉译文
  }, [text, pageWidth, y0, y1]);

  return (
    <div ref={box} className={`ovl${warn ? " warn" : ""}`} data-i={block.index}
      style={{ left: pct(x0), top: pct(y0), width: pct(x1 - x0), minHeight: pct(y1 - y0) }}>
      {sentences.map((s, k) => <span key={k}>{rich(s)} </span>)}
    </div>
  );
}

/** 某一段的覆盖状态：显示的句子、是否未通过核验；正在翻译、出错时只在段落角上标记。 */
function overlayState(tr: FlowTranslation, index: number) {
  const t = tr.translations;
  const live = t.live.get(index);
  if (live !== undefined) return { sentences: live ? [live] : null, mark: "running" as const };
  if (t.errors.has(index)) return { sentences: null, mark: "error" as const };
  const done = t.done.get(index);
  if (!done || done.sentences.length === 0) return { sentences: null, mark: done ? ("error" as const) : null };
  return { sentences: done.sentences, mark: null, warn: !done.verified || done.stale };
}

export function OverlayLayer({ blocks, pageWidth, tr, hovered }: {
  blocks: Block[]; pageWidth: number; tr: FlowTranslation; hovered: number | null;
}) {
  const t = tr.translations;
  const pending = blocks.filter((b) => !t.done.has(b.index) && !t.live.has(b.index)).map((b) => b.index);
  return (
    <>
      {pending.length > 0 && (
        <button className="ghost ovl-page" onClick={() => t.translateAll(pending)} title="按段落分批翻译，会产生模型费用">
          翻译本页 {pending.length} 段
        </button>
      )}
      {blocks.map((b) => {
        const state = overlayState(tr, b.index);
        const [, y0, x1] = b.bbox!;
        return (
          <div key={b.index} className={hovered === b.index ? "ovl-wrap peek" : "ovl-wrap"}>
            {state.sentences && <OverlayText block={b} pageWidth={pageWidth} sentences={state.sentences} warn={!!state.warn} />}
            {state.mark && (
              <button className={`ovl-mark ${state.mark}`} style={{ left: pct(x1), top: pct(y0) }}
                title={state.mark === "running" ? "正在翻译" : t.errors.get(b.index) ?? "译文没有通过核验，保留原文。点击重试"}
                disabled={state.mark === "running"} onClick={() => t.translate(b.index)}>
                {state.mark === "running" ? "译…" : "!"}
              </button>
            )}
            {/* 有译文但未通过核验或是旧版本：译文照常覆盖（虚线框），角上可以重译 */}
            {state.warn && (
              <button className="ovl-mark warn" style={{ left: pct(x1), top: pct(y0) }} title="译文未通过核验或是旧版本，点击重新翻译"
                onClick={() => t.translate(b.index)}>↻</button>
            )}
          </div>
        );
      })}
    </>
  );
}
