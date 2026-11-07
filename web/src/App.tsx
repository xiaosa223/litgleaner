import { Inbox } from "./Inbox";
import { Reader } from "./Reader";
import { useRoute } from "./router";
import { ToastProvider } from "./ui";

export function App() {
  const route = useRoute();
  return (
    <ToastProvider>
      {route.name === "paper" ? <Reader key={route.paperId} paperId={route.paperId} /> : <div className="app"><Inbox /></div>}
    </ToastProvider>
  );
}
