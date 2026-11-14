// 设置：模型、外观、阅读偏好、花费。偏好存在浏览器里；模型配置存在本机服务；渠道等要等后端提供接口后再加。
import { useEffect, useState } from "react";
import { api } from "../api";
import { ModelSetupForm } from "../ModelSetup";
import { taskSummary } from "../modelForm";
import { usePref, useTranslateMode } from "../prefs";
import type { View } from "../ReaderHeader";
import { Modal, useToast } from "../ui";
import { useAsync } from "../useAsync";

export type Theme = "system" | "light" | "dark";

function Seg<T extends string>({ value, options, onChange, label }: {
  value: T; options: [T, string][]; onChange: (value: T) => void; label: string;
}) {
  return (
    <div className="seg" role="group" aria-label={label}>
      {options.map(([key, text]) => <button key={key} aria-pressed={value === key} onClick={() => onChange(key)}>{text}</button>)}
    </div>
  );
}

/** 导出我的数据：只导出，不读回；用户写的、模型生成的、作者原文在文件里分开标明。 */
function ExportData() {
  const toast = useToast();
  const [pdfs, setPdfs] = useState(false);
  const [busy, setBusy] = useState(false);
  const run = async () => {
    setBusy(true);
    try {
      const blob = await api.exportMine(pdfs);
      const url = URL.createObjectURL(blob);
      const link = Object.assign(document.createElement("a"), { href: url, download: "litgleaner-export.zip" });
      link.click();
      setTimeout(() => URL.revokeObjectURL(url), 1000);
    } catch (cause) {
      toast(cause instanceof Error ? cause.message : "导出失败，请重试");
    } finally {
      setBusy(false);
    }
  };
  return (
    <div className="setting" id="export">
      <div>
        <b>导出我的数据</b>
        <span className="d">每篇论文、每个概念一个 Markdown 文件：笔记和高亮、你写的回答（含每次修改）、评价、掌握程度，另附 BibTeX。不调用模型。</span>
        <label className="d" style={{ display: "flex", gap: 6, alignItems: "center", marginTop: 6 }}>
          <input type="checkbox" checked={pdfs} onChange={(e) => setPdfs(e.target.checked)} />一并导出 PDF（文件会大很多）
        </label>
      </div>
      <button className="ghost" onClick={() => void run()} disabled={busy}>{busy ? "正在打包…" : "导出 zip"}</button>
    </div>
  );
}

export function Settings() {
  const [theme, setTheme] = usePref<Theme>("theme", "system");
  const [view, setView] = usePref<View>("view", "pdf");
  const [mode, setMode] = useTranslateMode();
  const [costs] = useAsync(() => api.costs(), []);
  const [models, reloadModels] = useAsync(() => api.models(), []);
  const [editing, setEditing] = useState(false);
  const rows = models.state === "ready" ? taskSummary(models.data) : [];
  const configured = rows.some((row) => row.status === "ready");
  useEffect(() => {
    if (window.location.hash === "#export") document.getElementById("export")?.scrollIntoView({ block: "center" });
  }, []);
  return (
    <div className="screen-in">
      <div className="screen-head"><h1>设置</h1></div>

      <h2 className="block-title">模型</h2>
      <div className="setting">
        <div>
          <b>大模型</b>
          <span className="d">翻译、提问、每日推荐要用到。不配置也能打开 PDF、高亮、记笔记和检索；第一次用到时也会提示你配置。</span>
        </div>
        <button className={configured ? "ghost" : "primary"} onClick={() => setEditing(true)}>{configured ? "修改" : "配置模型"}</button>
      </div>
      {models.state === "error" && <p className="hint-line bad">{models.message}</p>}
      {configured && (
        <div className="model-status" aria-label="各功能使用的模型">
          {rows.map((row) => (
            <div key={row.task} style={{ display: "contents" }}>
              <span>{row.label}</span>
              <span className={row.status === "ready" ? "ok" : row.status}>
                {row.status === "ready" ? `${row.model ?? ""}${row.budget !== null ? ` · 每天最多 $${row.budget}` : ""}`
                  : row.status === "demo" ? "演示模式（不调用真实模型）" : "未配置"}
              </span>
            </div>
          ))}
        </div>
      )}
      {editing && (
        <Modal title="配置模型" onClose={() => setEditing(false)} width={560}>
          <ModelSetupForm onCancel={() => setEditing(false)} onSaved={() => { setEditing(false); reloadModels(); }} />
        </Modal>
      )}

      <h2 className="block-title">外观</h2>
      <div className="setting">
        <div><b>主题</b><span className="d">跟随系统时，随系统的浅色、深色切换</span></div>
        <Seg label="主题" value={theme} onChange={setTheme} options={[["system", "跟随系统"], ["light", "浅色"], ["dark", "深色"]]} />
      </div>

      <h2 className="block-title">阅读</h2>
      <div className="setting">
        <div><b>打开论文时的视图</b><span className="d">原版保留 PDF 版式；段落视图重排文字，适合小屏</span></div>
        <Seg label="视图" value={view} onChange={setView} options={[["pdf", "原版"], ["flow", "段落"]]} />
      </div>
      <div className="setting">
        <div><b>打开论文时显示</b><span className="d">覆盖和对照会按段落翻译，翻译会产生模型费用；段落视图里覆盖按对照显示</span></div>
        <Seg label="翻译" value={mode} onChange={setMode} options={[["off", "原文"], ["overlay", "覆盖"], ["column", "对照"]]} />
      </div>

      <h2 className="block-title">数据</h2>
      <ExportData />

      <h2 className="block-title">模型花费</h2>
      <div className="setting">
        <div><b>本月累计</b><span className="d">翻译、讲解、提问等用到模型的操作</span></div>
        <span style={{ fontVariantNumeric: "tabular-nums" }}>
          {costs.state === "ready" ? `$${costs.data.month_usd.toFixed(2)}` : costs.state === "error" ? "读取失败" : "…"}
        </span>
      </div>
    </div>
  );
}
