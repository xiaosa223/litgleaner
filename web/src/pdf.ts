// 打开 PDF 并读取每页尺寸。原版视图和段落视图的配图裁剪共用同一个文档。
import { GlobalWorkerOptions, getDocument } from "pdfjs-dist";
import type { PDFDocumentProxy } from "pdfjs-dist";
import workerUrl from "pdfjs-dist/build/pdf.worker.min.mjs?url";
import { useEffect, useState } from "react";

GlobalWorkerOptions.workerSrc = workerUrl;

export interface PdfState {
  doc: PDFDocumentProxy;
  sizes: { width: number; height: number }[]; // 每页尺寸（点），缩放为 1 时
}

export function usePdf(url: string | null): { pdf: PdfState | null; error: string | null } {
  const [pdf, setPdf] = useState<PdfState | null>(null);
  const [error, setError] = useState<string | null>(null);
  useEffect(() => {
    if (!url) return;
    const task = getDocument({ url });
    let alive = true;
    task.promise
      .then(async (doc) => {
        const sizes = [];
        for (let n = 1; n <= doc.numPages; n += 1) {
          const viewport = (await doc.getPage(n)).getViewport({ scale: 1 });
          sizes.push({ width: viewport.width, height: viewport.height });
        }
        if (alive) setPdf({ doc, sizes });
      })
      .catch((reason: unknown) => alive && setError(`PDF 打开失败：${String(reason)}`));
    return () => {
      alive = false;
      void task.destroy();
    };
  }, [url]);
  return { pdf, error };
}
