// 入口：阅读页全屏显示，其余页面放在带导航的骨架里；添加论文和全局检索在任何页面都能用。
import { useEffect, useState } from "react";
import { AddPaper } from "./AddPaper";
import { api } from "./api";
import { Knowledge } from "./pages/Knowledge";
import { Library } from "./pages/Library";
import { Settings, type Theme } from "./pages/Settings";
import { Today } from "./pages/Today";
import { TopicPage, Topics } from "./pages/Topics";
import { usePref } from "./prefs";
import { Reader } from "./Reader";
import { useRoute } from "./router";
import { SearchPalette } from "./SearchPalette";
import { Shell } from "./Shell";
import { ErrorBoundary } from "./ErrorBoundary";
import { ToastProvider } from "./ui";
import { useAsync } from "./useAsync";

export function App() {
  const route = useRoute();
  const inReader = route.name === "paper";
  // 从阅读页回来时重新取一次：阅读状态可能变了
  const [inbox, reload] = useAsync(() => api.inbox(), [inReader]);
  const [adding, setAdding] = useState(false);
  const [searching, setSearching] = useState(false);
  const [theme] = usePref<Theme>("theme", "system");

  useEffect(() => {
    if (theme === "system") delete document.documentElement.dataset.theme;
    else document.documentElement.dataset.theme = theme;
  }, [theme]);

  useEffect(() => {
    const onKey = (event: KeyboardEvent) => {
      if ((event.metaKey || event.ctrlKey) && event.key.toLowerCase() === "k") {
        event.preventDefault();
        setSearching(true);
      }
    };
    // 把文件拖进窗口就打开“添加论文”
    const onDrag = (event: DragEvent) => {
      if (event.dataTransfer?.types.includes("Files")) setAdding(true);
    };
    window.addEventListener("keydown", onKey);
    window.addEventListener("dragenter", onDrag);
    return () => {
      window.removeEventListener("keydown", onKey);
      window.removeEventListener("dragenter", onDrag);
    };
  }, []);

  const add = () => setAdding(true);
  return (
    <ErrorBoundary>
    <ToastProvider>
      {route.name === "paper" ? <Reader key={route.paperId} paperId={route.paperId} /> : (
        <Shell route={route} onAdd={add} onSearch={() => setSearching(true)}>
          {route.name === "today" && <Today inbox={inbox} reload={reload} onAdd={add} />}
          {route.name === "topics" && <Topics inbox={inbox} />}
          {route.name === "topic" && <TopicPage key={route.topicId} topicId={route.topicId} inbox={inbox} reload={reload} />}
          {route.name === "library" && <Library onAdd={add} />}
          {route.name === "knowledge" && <Knowledge />}
          {route.name === "settings" && <Settings />}
        </Shell>
      )}
      {adding && <AddPaper onClose={() => setAdding(false)} />}
      {searching && <SearchPalette onClose={() => setSearching(false)} />}
    </ToastProvider>
    </ErrorBoundary>
  );
}
