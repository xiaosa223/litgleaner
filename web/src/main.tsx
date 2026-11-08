import { StrictMode } from "react";
import { createRoot } from "react-dom/client";
import { App } from "./App";
import { PdfEngineProvider } from "./pdf/EngineProvider";
import "./styles.css";
import "./tailwind.css";

const root = document.getElementById("root");
if (!root) throw new Error("页面缺少 #root");
createRoot(root).render(
  <StrictMode>
    <PdfEngineProvider>
      <App />
    </PdfEngineProvider>
  </StrictMode>,
);
