import { SOON } from "../ui";

export function QaTab() {
  return (
    <>
      <div className="msgs">
        <div className="tag">回答会引用原文并标明页码；没有原文依据的部分标为“模型推断”。</div>
      </div>
      <div className="soon">{SOON.ask}</div>
    </>
  );
}
