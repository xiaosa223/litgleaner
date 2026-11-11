// 页面骨架：左侧常驻导航（今天、课题、文库、知识库、设置）、添加论文和全局检索（第 12.8 节）。
import { BookOpen, Compass, Lightbulb, Plus, Search, Settings as SettingsIcon, Sun, type LucideIcon } from "lucide-react";
import type { MouseEvent, ReactNode } from "react";
import { MOD } from "./platform";
import { usePreview } from "./preview/usePreview";
import { navigate, paths, type Route } from "./router";

const NAV: { name: Route["name"]; also?: Route["name"]; label: string; path: string; icon: LucideIcon }[] = [
  { name: "today", label: "今天", path: paths.today, icon: Sun },
  { name: "topics", also: "topic", label: "课题", path: paths.topics, icon: Compass },
  { name: "library", label: "文库", path: paths.library, icon: BookOpen },
  { name: "knowledge", label: "知识库", path: paths.knowledge, icon: Lightbulb },
];

function NavLink({ path, label, icon: Icon, active }: { path: string; label: string; icon: LucideIcon; active: boolean }) {
  const go = (event: MouseEvent) => {
    if (event.metaKey || event.ctrlKey || event.shiftKey) return; // 新标签页打开交给浏览器
    event.preventDefault();
    navigate(path);
  };
  return (
    <a className="nav-item" href={path} onClick={go} aria-current={active ? "page" : undefined} title={label}>
      <Icon size={17} aria-hidden /><span className="nav-lbl">{label}</span>
    </a>
  );
}

export function Shell({ route, onAdd, onSearch, children }: {
  route: Route; onAdd: () => void; onSearch: () => void; children: ReactNode;
}) {
  const [preview] = usePreview();
  return (
    <div className="shell">
      <nav className="side-nav" aria-label="主导航">
        <div className="brand">拾穗<small>LitGleaner</small></div>
        <button className="nav-btn add" onClick={onAdd} title="添加论文">
          <Plus size={17} aria-hidden /><span className="nav-lbl">添加论文</span>
        </button>
        <button className="nav-btn find" onClick={onSearch} title={`搜索（${MOD} K）`}>
          <Search size={16} aria-hidden /><span className="nav-lbl grow">搜索</span><kbd>{MOD} K</kbd>
        </button>
        <div className="nav-list">
          {NAV.map((item) => (
            <NavLink key={item.name} path={item.path} label={item.label} icon={item.icon}
              active={route.name === item.name || route.name === item.also} />
          ))}
        </div>
        <div className="nav-foot">
          {preview && <div className="nav-note">界面预览已开启：标着“示例数据”的内容还没有接上服务。可以在设置里关掉。</div>}
          <NavLink path={paths.settings} label="设置" icon={SettingsIcon} active={route.name === "settings"} />
        </div>
      </nav>
      <main className="screen">{children}</main>
    </div>
  );
}
