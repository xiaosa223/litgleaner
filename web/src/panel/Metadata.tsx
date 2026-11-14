// 论文信息补全（ADR 0011）：上传的 PDF 认不出标识符时，标题是文件名。用户确认或修改后，
// 只在界面和个人导出里覆盖显示，来源元数据保持不变。
import { useEffect, useState } from "react";
import { api } from "../api";
import type { MetadataField, MetadataView } from "../guidance";
import type { PaperView } from "../types";
import { Modal, useToast } from "../ui";

const FIELDS: [MetadataField, string, string][] = [
  ["title", "标题", ""],
  ["authors", "作者", "用顿号或分号分隔"],
  ["venue", "期刊或会议", ""],
  ["published_at", "年份", "例如 2016"],
];

function Editor({ paper, initial, onClose, onSaved }: {
  paper: PaperView; initial: Record<MetadataField, string>; onClose: () => void; onSaved: () => void;
}) {
  const toast = useToast();
  const [values, setValues] = useState(initial);
  const [saving, setSaving] = useState(false);
  const save = async () => {
    setSaving(true);
    try {
      // 只提交改过的字段：没动的来源元数据不被“钉”成用户修正，也不会因格式不合被拒
      const changed = Object.fromEntries(FIELDS.map(([key]) => key).filter((key) => values[key].trim() !== initial[key].trim()).map((key) => [key, values[key].trim()]));
      if (Object.keys(changed).length > 0) await api.saveMetadata(paper.paper_id, changed);
      onSaved();
      onClose();
    } catch (cause) {
      toast(cause instanceof Error ? cause.message : "没能保存，请重试");
    } finally {
      setSaving(false);
    }
  };
  return (
    <Modal title="补全论文信息" onClose={onClose} width={480}>
      <p className="tag" style={{ marginTop: 0 }}>只改你看到的显示，不会改动从学术数据库查到的原始信息；清空某一项就恢复原样。</p>
      {FIELDS.map(([key, label, hint]) => (
        <label key={key} className="meta-field">
          <span>{label}</span>
          <input className="field" value={values[key]} placeholder={hint}
            onChange={(e) => setValues({ ...values, [key]: e.target.value })} />
        </label>
      ))}
      <div className="row" style={{ justifyContent: "flex-end" }}>
        <button className="ghost" onClick={onClose}>取消</button>
        <button className="primary sm" onClick={() => void save()} disabled={saving}>保存</button>
      </div>
    </Modal>
  );
}

export function MetadataPrompt({ paper, onSaved }: { paper: PaperView; onSaved: () => void }) {
  const [view, setView] = useState<MetadataView | null>(null);
  const [editing, setEditing] = useState(false);
  const [version, setVersion] = useState(0);
  useEffect(() => {
    let alive = true;
    api.metadata(paper.paper_id).then((v) => alive && setView(v), () => undefined);
    return () => { alive = false; };
  }, [paper.paper_id, version]);
  const initial: Record<MetadataField, string> = {
    title: view?.corrected.title ?? view?.title_suggestion ?? paper.title,
    authors: view?.corrected.authors ?? paper.authors.join("、"),
    venue: view?.corrected.venue ?? paper.venue ?? "",
    published_at: view?.corrected.published_at ?? paper.published_at ?? "",
  };
  const done = () => { setVersion((v) => v + 1); onSaved(); };
  const needsTitle = view?.pending && !view.corrected.title;
  return (
    <>
      {needsTitle ? (
        <div className="meta-prompt" role="note">
          <div>没查到这篇的信息，现在显示的是文件名。补上标题，首页和笔记里就能认出它。</div>
          <div className="row" style={{ marginTop: 6 }}>
            <button className="primary sm" onClick={() => setEditing(true)}>补全信息</button>
            {view.title_suggestion && <span className="tag">已按首页文字预填，可能连着作者名，请删掉多余部分</span>}
          </div>
        </div>
      ) : (
        <button className="link-btn" onClick={() => setEditing(true)}>编辑信息</button>
      )}
      {editing && <Editor paper={paper} initial={initial} onClose={() => setEditing(false)} onSaved={done} />}
    </>
  );
}
