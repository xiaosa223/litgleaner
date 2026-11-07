// 选中文字后点“翻译”：显示所选内容所在句的原文和译文；这段还没翻译时可以就地翻译。
import { rich } from "./text";
import type { Translations } from "./translations";
import type { TextSelection } from "./selection";

const POP_WIDTH = 380;
const norm = (s: string) => s.normalize("NFKC").replace(/\$[^$]*\$/g, " ").toLowerCase().replace(/[^a-z0-9]/g, "");

/** 所选文字覆盖了哪几句：按去掉空白和符号后的位置比对。 */
export function sentencesFor(sources: string[], text: string): number[] {
  const norms = sources.map(norm);
  const offsets: number[] = [];
  let joined = "";
  norms.forEach((n) => { offsets.push(joined.length); joined += n; });
  let needle = norm(text);
  let at = joined.indexOf(needle);
  if (at < 0 && needle.length > 24) { needle = needle.slice(0, 24); at = joined.indexOf(needle); }
  if (at < 0) return [];
  const end = at + needle.length;
  return norms.map((n, k) => [offsets[k], offsets[k] + n.length, k]).filter(([a, b]) => a < end && b > at).map(([, , k]) => k);
}

export function SelectionPopup({ selection, translations, onClose, onOpenBlock, onNote, onAsk }: {
  selection: TextSelection; translations: Translations; onClose: () => void;
  onOpenBlock: (index: number) => void; onNote: () => void; onAsk: () => void;
}) {
  const block = selection.block;
  const tr = block ? translations.done.get(block.index) : undefined;
  const live = block ? translations.live.get(block.index) : undefined;
  const error = block ? translations.errors.get(block.index) : undefined;
  const hits = tr ? sentencesFor(tr.sources, selection.text) : [];
  const left = Math.max(12, Math.min(selection.anchor.x - POP_WIDTH / 2, window.innerWidth - POP_WIDTH - 12));
  const top = selection.anchor.y + 12;

  let body;
  if (!block) body = <div className="tag">没能定位到所在段落。</div>;
  else if (!block.translatable) body = <div className="tag">这段不需要翻译（标题、公式、表格或中文段落）。</div>;
  else if (live !== undefined) body = <div className="zhs">{live || "正在翻译…"}</div>;
  else if (error) body = <div className="tag" style={{ color: "var(--warn)" }}>{error}</div>;
  else if (!tr) {
    body = (
      <>
        <div className="src">“{selection.text.slice(0, 160)}”</div>
        <button className="primary sm" onClick={() => translations.translate(block.index)}>翻译这段</button>
      </>
    );
  } else if (!hits.length) body = <div className="tag">没能把选中内容对应到句子，可以看整段译文。</div>;
  else body = hits.map((k) => (
    <div key={k}><div className="src">{rich(tr.sources[k])}</div><div className="zhs">{rich(tr.sentences[k])}</div></div>
  ));

  return (
    <div className="selpop" style={{ left, top }} onMouseDown={(e) => e.stopPropagation()}>
      <button className="x" aria-label="关闭" onClick={onClose}>×</button>
      <div className="tag" style={{ marginBottom: 6 }}>
        {hits.length > 1 ? `所选内容跨 ${hits.length} 句` : "所选内容所在句"} · 第 {(selection.page ?? 0) + 1} 页
      </div>
      {body}
      <div className="row">
        {block && tr && <button className="ghost" onClick={() => onOpenBlock(block.index)}>整段译文</button>}
        <button className="ghost" onClick={onAsk}>问这句</button>
        <button className="ghost" onClick={onNote}>记下来</button>
      </div>
      {tr && !tr.verified && <div className="tag" style={{ marginTop: 6, color: "var(--warn)" }}>这段译文未通过核验，仅供参考。</div>}
    </div>
  );
}
