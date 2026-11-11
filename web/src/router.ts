// 用 History API 切换页面，不引入路由库；地址的解析在 routes.ts。
import { useEffect, useState } from "react";
import { parseRoute, paths, type Route } from "./routes";

export { paths, type Route };

export function navigate(path: string): void {
  window.history.pushState({ app: true }, "", path);
  window.dispatchEvent(new PopStateEvent("popstate"));
}

/** 从应用内的页面进来就返回上一页；直接打开的链接没有上一页，去备用地址。 */
export function goBack(fallback: string): void {
  if ((window.history.state as { app?: boolean } | null)?.app) window.history.back();
  else navigate(fallback);
}

export function useRoute(): Route {
  const [route, setRoute] = useState(() => parseRoute(window.location.pathname));
  useEffect(() => {
    const update = () => setRoute(parseRoute(window.location.pathname));
    window.addEventListener("popstate", update);
    return () => window.removeEventListener("popstate", update);
  }, []);
  return route;
}
