import { headingLevel, isReferencesHeading } from "./text";
import type { Block } from "./types";

export function Toc({ blocks, active, onJump }: { blocks: Block[]; active: number | null; onJump: (block: Block) => void }) {
  const headings: Block[] = [];
  for (const b of blocks) {
    if (b.kind !== "heading") continue;
    headings.push(b);
    if (isReferencesHeading(b.text)) break;
  }
  return (
    <nav className="toc" aria-label="目录">
      {headings.length === 0 && <div className="empty-note" style={{ padding: 8 }}>没有解析出章节</div>}
      {headings.map((h) => (
        <a key={h.index} className={`l${headingLevel(h.text)}${active === h.index ? " active" : ""}`} onClick={() => onJump(h)}>
          {h.text}
        </a>
      ))}
    </nav>
  );
}
