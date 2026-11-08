// PDF 引擎（PDFium wasm）的创建与销毁。优先在 worker 里运行，worker 起不来时退回主线程。
// 改写自 Agentero 的 src/lib/pdf/engine.ts（MIT，见 THIRD_PARTY.md）：去掉了系统中文字体后备
// （那边经 Tauri 读取；我们以后由本地接口提供，见 ROADMAP）和日志模块。
import { ignore, type PdfEngine } from "@embedpdf/models";
import pdfiumWasmUrl from "@embedpdf/pdfium/pdfium.wasm?url";

/** 打过补丁的 worker 引擎暴露了初始化握手。 */
type WorkerPdfEngine = PdfEngine & {
  whenReady(): { toPromise(): Promise<unknown> };
};

const WORKER_READY_TIMEOUT_MS = 8000;
let workerUsable = true;

/** 启动失败时不能把 closeDocument 排在一个永远不结束的握手后面。 */
function destroyEngine(engine: PdfEngine): void {
  engine.destroy?.().wait(ignore, ignore);
}

/** 先关闭打开的文档，再销毁引擎。 */
export function disposePdfEngine(engine: PdfEngine): void {
  engine.closeAllDocuments().wait(
    () => destroyEngine(engine),
    () => destroyEngine(engine),
  );
}

async function waitWorkerReady(engine: WorkerPdfEngine, signal: AbortSignal): Promise<void> {
  let timer: ReturnType<typeof setTimeout> | undefined;
  let onAbort: (() => void) | undefined;
  try {
    signal.throwIfAborted();
    await Promise.race([
      engine.whenReady().toPromise(),
      new Promise<never>((_, reject) => {
        timer = setTimeout(() => reject(new Error("PDF 引擎的 worker 启动超时")), WORKER_READY_TIMEOUT_MS);
        onAbort = () => reject(signal.reason);
        signal.addEventListener("abort", onAbort, { once: true });
      }),
    ]);
    signal.throwIfAborted();
  } finally {
    clearTimeout(timer);
    if (onAbort) signal.removeEventListener("abort", onAbort);
  }
}

export async function createPdfEngine(signal: AbortSignal): Promise<PdfEngine> {
  signal.throwIfAborted();
  if (workerUsable) {
    let worker: PdfEngine | undefined;
    try {
      const { createPdfiumEngine } = await import("@embedpdf/engines/pdfium-worker-engine");
      signal.throwIfAborted();
      worker = createPdfiumEngine(new URL(pdfiumWasmUrl, document.baseURI).href);
      await waitWorkerReady(worker as WorkerPdfEngine, signal);
      return worker;
    } catch {
      if (worker) destroyEngine(worker);
      signal.throwIfAborted(); // 组件卸载不说明 worker 不可用
      workerUsable = false; // 退回主线程引擎，照常可用，只是渲染占主线程
    }
  }
  const { createPdfiumEngine } = await import("@embedpdf/engines/pdfium-direct-engine");
  signal.throwIfAborted();
  const engine = await createPdfiumEngine(pdfiumWasmUrl);
  if (signal.aborted) {
    destroyEngine(engine);
    signal.throwIfAborted();
  }
  return engine;
}
