// 导读：背景卡、章节导读、前置阅读都是跳回原文的入口，不提供“读完卡片就够了”的路径。
import { headingLevel, isReferencesHeading } from "../text";
import type { Block } from "../types";
import { SOON } from "../ui";

export function GuideTab({ blocks, onJump }: { blocks: Block[]; onJump: (block: Block) => void }) {
  const sections: Block[] = [];
  for (const b of blocks) {
    if (b.kind !== "heading") continue;
    if (isReferencesHeading(b.text)) break;
    if (headingLevel(b.text) === 2) sections.push(b);
  }
  return (
    <>
      <div className="sec">
        <h4>原理动画 <span className="tag">默认关闭</span></h4>
        <div className="anim">
          <div className="sub">用一个小例子分步演示这篇论文的核心机制。生成分镜要消耗较多 token，所以默认关闭，每篇手动生成。</div>
          <div className="row"><button className="primary sm" disabled title="原理动画还在开发中">生成原理动画</button></div>
        </div>
      </div>
      <div className="sec">
        <h4>这篇要解决什么 · 先懂这几个概念 · 章节导读 · 前置阅读</h4>
        <div className="soon">{SOON.guide}</div>
      </div>
      <div className="sec">
        <h4>章节</h4>
        {sections.length ? (
          <ul className="list">
            {sections.map((s) => <li key={s.index}><span className="link" onClick={() => onJump(s)}>{s.text}</span></li>)}
          </ul>
        ) : (
          <div className="empty-note">没有解析出章节结构，请用原版视图阅读。</div>
        )}
      </div>
    </>
  );
}
