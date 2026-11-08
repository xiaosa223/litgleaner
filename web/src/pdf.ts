// 读取 PDF 并在引擎里打开，得到每页尺寸。原版视图和段落视图的配图裁剪共用这一份。
import type { PdfDocumentObject, PdfEngine } from "@embedpdf/models";
import { useEffect, useState } from "react";
import { usePdfEngine } from "./pdf/EngineProvider";

export interface PdfState {
  id: string;
  engine: PdfEngine;
  doc: PdfDocumentObject; // 用于裁图；原版视图另开一份交给 EmbedPDF 管理
  bytes: ArrayBuffer; // 原始字节，给原版视图用时复制一份（worker 会接管传入的缓冲区）
  sizes: { width: number; height: number }[]; // 每页尺寸（点）
}

let openCount = 0;

export function usePdf(url: string | null): { pdf: PdfState | null; error: string | null } {
  const { engine, error: engineError } = usePdfEngine();
  const [pdf, setPdf] = useState<PdfState | null>(null);
  const [error, setError] = useState<string | null>(null);
  useEffect(() => {
    if (!url || !engine) return;
    const id = `crop:${url}:${(openCount += 1)}`; // 每次打开用不同编号，切换论文时晚到的打开不会和新的混在一起
    const controller = new AbortController();
    let opened: PdfDocumentObject | null = null;
    const close = (doc: PdfDocumentObject) => engine.closeDocument(doc).wait(() => undefined, () => undefined);
    (async () => {
      const response = await fetch(url, { credentials: "same-origin", signal: controller.signal });
      if (!response.ok) throw new Error(`下载失败（${response.status}）`);
      const bytes = await response.arrayBuffer();
      const doc = await engine.openDocumentBuffer({ id, content: bytes.slice(0) }).toPromise();
      if (controller.signal.aborted) return close(doc); // 已经离开这篇论文：晚到的文档要关掉，否则一直留在引擎里
      opened = doc;
      const sizes = doc.pages.map((page) => ({ width: page.size.width, height: page.size.height }));
      setPdf({ id, engine, doc, bytes, sizes });
    })().catch((reason: unknown) => !controller.signal.aborted && setError(`PDF 打开失败：${String(reason)}`));
    return () => {
      controller.abort();
      if (opened) close(opened);
    };
  }, [url, engine]);
  return { pdf, error: error ?? engineError };
}
