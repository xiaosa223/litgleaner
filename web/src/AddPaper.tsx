// 添加论文：贴 DOI、arXiv 编号、链接，或拖入 PDF（第 12.8 节）。任何页面都能打开。
import { useCallback, useEffect, useRef, useState, type DragEvent } from "react";
import { api, isNotConnected, NOT_CONNECTED } from "./api";
import { identify } from "./identify";
import { navigate, paths } from "./router";
import { Modal } from "./ui";

const MAX_BYTES = 100 * 1024 * 1024;
const KIND = { doi: "DOI", arxiv: "arXiv 编号", url: "链接" } as const;

function checkFile(file: File): string | null {
  if (file.type !== "application/pdf" && !file.name.toLowerCase().endsWith(".pdf")) return "只能添加 PDF 文件。";
  if (file.size > MAX_BYTES) return "文件超过 100 MB。";
  return null;
}

export function AddPaper({ onClose }: { onClose: () => void }) {
  const [text, setText] = useState("");
  const [file, setFile] = useState<File | null>(null);
  const [over, setOver] = useState(false);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const picker = useRef<HTMLInputElement>(null);
  const found = identify(text);

  const take = useCallback((picked: File | undefined) => {
    if (!picked) return;
    const problem = checkFile(picked);
    setError(problem);
    setFile(problem ? null : picked);
  }, []);

  // 弹窗开着时，拖到窗口任何位置都算数；同时阻止浏览器直接打开这个文件
  useEffect(() => {
    const onDrop = (event: globalThis.DragEvent) => {
      event.preventDefault();
      setOver(false);
      take(event.dataTransfer?.files[0]);
    };
    const onOver = (event: globalThis.DragEvent) => event.preventDefault();
    window.addEventListener("drop", onDrop);
    window.addEventListener("dragover", onOver);
    return () => {
      window.removeEventListener("drop", onDrop);
      window.removeEventListener("dragover", onOver);
    };
  }, [take]);

  const submit = async () => {
    if (!file && !found) return;
    setBusy(true);
    setError(null);
    try {
      const added = file ? await api.uploadPaper(file) : await api.addPaper(found!);
      onClose();
      navigate(paths.paper(added.paper_id));
    } catch (problem) {
      setError(isNotConnected(problem) ? NOT_CONNECTED : problem instanceof Error ? problem.message : "添加失败");
    } finally {
      setBusy(false);
    }
  };

  const hint = file ? null
    : !text.trim() ? null
    : found ? { cls: "ok", text: `识别为${KIND[found.kind]}：${found.value}` }
    : { cls: "bad", text: "认不出这是什么。可以贴 DOI（10. 开头）、arXiv 编号（如 1706.03762）或以 http 开头的论文链接。" };
  const drag = { onDragEnter: (e: DragEvent) => { e.preventDefault(); setOver(true); }, onDragLeave: () => setOver(false) };

  return (
    <Modal onClose={onClose} title="添加论文" width={520}>
      <div className={`drop${over ? " over" : ""}`} {...drag}>
        {file
          ? <><b>{file.name}</b> · {(file.size / 1024 / 1024).toFixed(1)} MB <button className="linkbtn" onClick={() => setFile(null)}>换一个</button></>
          : <>把 PDF 拖到这里，或者 <button className="linkbtn" onClick={() => picker.current?.click()}>选择文件</button></>}
        <input ref={picker} type="file" accept="application/pdf,.pdf" hidden onChange={(e) => take(e.target.files?.[0])} />
      </div>
      {!file && (
        <>
          <div className="or">或者</div>
          <div className="form-row">
            <label htmlFor="paper-id">DOI、arXiv 编号或论文链接</label>
            <input id="paper-id" className="field" autoFocus value={text} placeholder="10.1038/nature14539 · 1706.03762 · https://…"
              onChange={(e) => { setText(e.target.value); setError(null); }}
              onKeyDown={(e) => { if (e.key === "Enter" && found && !busy) void submit(); }} />
          </div>
        </>
      )}
      <div className={`hint-line ${error ? "bad" : hint?.cls ?? ""}`} role="status">{error ?? hint?.text}</div>
      <div className="tag">标题、作者、期刊只从学术数据源查，不由模型填写。</div>
      <div className="dialog-actions">
        <button className="ghost" onClick={onClose}>取消</button>
        <button className="primary" disabled={busy || (!file && !found)} onClick={() => void submit()}>{busy ? "正在添加…" : "添加并打开"}</button>
      </div>
    </Modal>
  );
}
