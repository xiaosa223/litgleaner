// 整个页面共用一个 PDF 引擎：原版视图、段落视图的配图裁剪、以后的译文面板都从这里取。
// 改写自 Agentero 的 src/components/viewer/pdf/engine-provider.tsx（MIT，见 THIRD_PARTY.md）。
import type { PdfEngine } from "@embedpdf/models";
import { createContext, useContext, useEffect, useState, type ReactNode } from "react";
import { createPdfEngine, disposePdfEngine } from "./engine";

interface EngineState {
  engine: PdfEngine | null;
  loading: boolean;
  error: string | null;
}

const EngineContext = createContext<EngineState>({ engine: null, loading: true, error: null });

export function PdfEngineProvider({ children }: { children: ReactNode }) {
  const [state, setState] = useState<EngineState>({ engine: null, loading: true, error: null });

  useEffect(() => {
    const controller = new AbortController();
    let current: PdfEngine | null = null;
    createPdfEngine(controller.signal)
      .then((engine) => {
        if (controller.signal.aborted) return disposePdfEngine(engine);
        current = engine;
        setState({ engine, loading: false, error: null });
      })
      .catch((error: unknown) => {
        if (controller.signal.aborted) return;
        setState({ engine: null, loading: false, error: `PDF 引擎启动失败：${String(error)}` });
      });
    return () => {
      controller.abort();
      if (current) disposePdfEngine(current);
      current = null;
    };
  }, []);

  return <EngineContext.Provider value={state}>{children}</EngineContext.Provider>;
}

export function usePdfEngine(): EngineState {
  return useContext(EngineContext);
}
