// 只有两个页面，用 History API 即可，不引入路由库。
import { useEffect, useState } from "react";

export type Route = { name: "home" } | { name: "paper"; paperId: string };

function parse(pathname: string): Route {
  const match = /^\/papers\/([^/]+)\/?$/.exec(pathname);
  return match ? { name: "paper", paperId: decodeURIComponent(match[1]) } : { name: "home" };
}

export function navigate(path: string): void {
  window.history.pushState(null, "", path);
  window.dispatchEvent(new PopStateEvent("popstate"));
}

export function paperPath(paperId: string): string {
  return `/papers/${encodeURIComponent(paperId)}`;
}

export function useRoute(): Route {
  const [route, setRoute] = useState(() => parse(window.location.pathname));
  useEffect(() => {
    const update = () => setRoute(parse(window.location.pathname));
    window.addEventListener("popstate", update);
    return () => window.removeEventListener("popstate", update);
  }, []);
  return route;
}
