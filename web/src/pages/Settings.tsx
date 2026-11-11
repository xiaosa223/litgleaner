// 设置：外观、阅读偏好、花费、界面预览。偏好存在浏览器里；渠道、模型等要等后端提供接口后再加。
import { api } from "../api";
import { usePref, useTranslateMode } from "../prefs";
import { usePreview } from "../preview/usePreview";
import type { View } from "../ReaderHeader";
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

export function Settings() {
  const [theme, setTheme] = usePref<Theme>("theme", "system");
  const [view, setView] = usePref<View>("view", "pdf");
  const [mode, setMode] = useTranslateMode();
  const [preview, setPreview] = usePreview();
  const [costs] = useAsync(() => api.costs(), []);
  return (
    <div className="screen-in">
      <div className="screen-head"><h1>设置</h1></div>

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

      <h2 className="block-title">模型花费</h2>
      <div className="setting">
        <div><b>本月累计</b><span className="d">翻译、讲解、提问等用到模型的操作</span></div>
        <span style={{ fontVariantNumeric: "tabular-nums" }}>
          {costs.state === "ready" ? `$${costs.data.month_usd.toFixed(2)}` : costs.state === "error" ? "读取失败" : "…"}
        </span>
      </div>

      <h2 className="block-title">开发中</h2>
      <div className="setting">
        <div><b>界面预览</b><span className="d">用示例数据展示还没有接上服务的页面：课题调研、知识库、边读边问</span></div>
        <button className="switch" role="switch" aria-checked={preview} aria-label="界面预览" onClick={() => setPreview(!preview)} />
      </div>
    </div>
  );
}
