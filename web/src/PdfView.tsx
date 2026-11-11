// 原版视图：EmbedPDF（PDFium wasm）负责滚动、虚拟化、渲染和按字形选字（ADR 0006）；
// 每页的段落区域、高亮和译文栏见 PdfPage。插件配置参照 Agentero 的 pdf-viewer.tsx（MIT，见 THIRD_PARTY.md）。
import { createPluginRegistration } from "@embedpdf/core";
import { EmbedPDF, useDocumentState } from "@embedpdf/core/react";
import { DocumentContent, DocumentManagerPluginPackage } from "@embedpdf/plugin-document-manager/react";
import { GlobalPointerProvider, InteractionManagerPluginPackage } from "@embedpdf/plugin-interaction-manager/react";
import { RenderPluginPackage } from "@embedpdf/plugin-render/react";
import { ScrollPluginPackage, Scroller, useScroll, useScrollCapability } from "@embedpdf/plugin-scroll/react";
import { SelectionPluginPackage, useSelectionCapability } from "@embedpdf/plugin-selection/react";
import { TilingPluginPackage } from "@embedpdf/plugin-tiling/react";
import { Viewport, ViewportPluginPackage } from "@embedpdf/plugin-viewport/react";
import { ZoomMode, ZoomPluginPackage } from "@embedpdf/plugin-zoom/react";
import { useEffect, useMemo, useRef, useState, type MouseEvent, type MutableRefObject } from "react";
import type { FlowTranslation } from "./FlowView";
import type { PdfState } from "./pdf";
import { COLUMN_GAP, DOC_ID, PdfPage, hitBlock, type BlockAction, type Focus } from "./PdfPage";
import { pdfSelection, type TextSelection } from "./selection";
import type { Annotation, Block } from "./types";

export { hitBlock, isBody, type BlockAction, type Focus } from "./PdfPage";

const MAX_PAGE_WIDTH = 900;
const COLUMN_WIDTH = 440; // 双语对照时页面右侧的译文栏
const ACTS_GUTTER = 70; // 段落右侧“译 / 问 / 记”按钮的位置

interface ViewProps {
  pdf: PdfState; blocks: Block[]; annotations: Annotation[]; focus: Focus | null;
  onAction: (action: BlockAction, block: Block) => void; translation: FlowTranslation;
  onPage: (page: number) => void; onSelect: (selection: TextSelection | null) => void; canAsk: boolean;
}

type PagesProps = ViewProps & {
  page: MutableRefObject<number>; gutter: number; hovered: number | null; onHover: (i: number | null) => void;
};

function Pages({ blocks, annotations, focus, onAction, translation, onPage, onSelect, page, gutter, hovered, onHover, canAsk }: PagesProps) {
  // useScroll 每次渲染都返回新的 scope 对象，不能放进 effect 依赖（否则每次重渲染都会重新跳页，
  // 拖选会被打断）。和 Agentero 一样用 ref 持有，effect 只依赖“是否就绪”。
  const { provides: scroll } = useScroll(DOC_ID);
  const { provides: scrollCapability } = useScrollCapability();
  const scrollRef = useRef(scroll);
  scrollRef.current = scroll;
  const [layoutReady, setLayoutReady] = useState(false);
  const ready = Boolean(scroll) && layoutReady;
  const { provides: selectionCap } = useSelectionCapability();
  const pages = useDocumentState(DOC_ID)?.document?.pages;
  const sizes = useMemo(() => pages?.map((p) => p.size) ?? [], [pages]);
  const latest = useRef({ sizes, blocks, onSelect, onPage });
  latest.current = { sizes, blocks, onSelect, onPage };
  const [focused, setFocused] = useState<number | null>(null);

  // 能拿到 capability 时 Scroller 的 DOM 还可能未布局。此时 scrollToPage 会被
  // 浏览器截为 0；等插件的可回放 layout-ready 事件再恢复位置或执行搜索跳转。
  useEffect(() => {
    if (!scrollCapability) return;
    return scrollCapability.onLayoutReady((event) => {
      if (event.documentId === DOC_ID) setLayoutReady(true);
    });
  }, [scrollCapability]);

  useEffect(() => {
    const current = scrollRef.current;
    if (!current || !ready) return;
    if (page.current > 0) current.scrollToPage({ pageNumber: page.current + 1, behavior: "instant" });
    return current.onPageChange((event) => {
      page.current = event.pageNumber - 1;
      latest.current.onPage(page.current);
    });
  }, [ready, page]);

  useEffect(() => {
    if (!selectionCap) return;
    const scope = selectionCap.forDocument(DOC_ID);
    let latestRun = 0;
    const offEnd = scope.onEndSelection(() => {
      const run = (latestRun += 1); // 取文字是异步的：连续选两次时只认最后一次
      const { sizes: s, blocks: b } = latest.current;
      void pdfSelection(scope, s, b).then((sel) => run === latestRun && latest.current.onSelect(sel));
    });
    return () => {
      latestRun += 1; // 卸载后晚到的结果也丢掉
      offEnd();
    };
  }, [selectionCap]);

  useEffect(() => {
    const current = scrollRef.current;
    if (!focus || !current || !ready) return;
    const { blocks: b, sizes: s } = latest.current;
    const block = b.find((x) => x.index === focus.block);
    const y = block?.bbox ? (block.bbox[1] / 1000) * (s[focus.page]?.height ?? 0) : 0;
    current.scrollToPage({ pageNumber: focus.page + 1, pageCoordinates: { x: 0, y: Math.max(0, y - 40) }, behavior: "smooth" });
    setFocused(focus.block);
    const timer = window.setTimeout(() => setFocused(null), 1800);
    return () => window.clearTimeout(timer);
  }, [focus, ready]);

  const columnWidth = translation.bilingual ? COLUMN_WIDTH : 0;
  return (
    <Viewport documentId={DOC_ID} className="pdf-viewport">
      {/* 拖选时指针移出页面也不中断 */}
      {/* 缩放按右侧留白算页宽，但滚动内容仍在整个视口里居中；左移半个留白，译文栏和按钮才放得下 */}
      <GlobalPointerProvider documentId={DOC_ID} style={{ position: "relative", left: -gutter / 2 }}>
        <Scroller documentId={DOC_ID} renderPage={(layout) => (
          <PdfPage index={layout.pageIndex} width={layout.width} height={layout.height} blocks={blocks} annotations={annotations}
            hovered={hovered} focused={focused} onAction={onAction} tr={translation} columnWidth={columnWidth} onHover={onHover} canAsk={canAsk} />
        )} />
      </GlobalPointerProvider>
    </Viewport>
  );
}

export function PdfView(props: ViewProps) {
  const { pdf, blocks, translation } = props;
  const page = useRef(0); // 切换双语对照会重建查看器，用它回到原来的页
  const [hovered, setHovered] = useState<number | null>(null);
  const gutter = translation.bilingual ? COLUMN_WIDTH + COLUMN_GAP : ACTS_GUTTER;
  const plugins = useMemo(() => [
    createPluginRegistration(DocumentManagerPluginPackage, {
      initialDocuments: [{ buffer: pdf.bytes.slice(0), documentId: DOC_ID, name: DOC_ID }],
    }),
    createPluginRegistration(ViewportPluginPackage),
    createPluginRegistration(ScrollPluginPackage, { defaultBufferSize: 2 }),
    createPluginRegistration(RenderPluginPackage),
    createPluginRegistration(TilingPluginPackage, { tileSize: 512 }),
    createPluginRegistration(ZoomPluginPackage, { defaultZoomLevel: ZoomMode.FitWidth, viewportPaddingRight: gutter }),
    createPluginRegistration(InteractionManagerPluginPackage),
    // 只选文字：框选容易在字形边缘误触，画出大块蓝框
    createPluginRegistration(SelectionPluginPackage, { marquee: { enabled: false } }),
  ], [pdf.bytes, gutter]);

  const onMove = (event: MouseEvent) => {
    const element = (event.target as HTMLElement).closest<HTMLElement>(".page");
    if (!element) return setHovered(null);
    const r = element.getBoundingClientRect();
    const hit = hitBlock(blocks, Number(element.dataset.p), ((event.clientX - r.left) / r.width) * 1000, ((event.clientY - r.top) / r.height) * 1000);
    setHovered(hit?.index ?? null);
  };

  return (
    <div className="pdf-host" style={{ maxWidth: MAX_PAGE_WIDTH + gutter }} onMouseMove={onMove} onMouseLeave={() => setHovered(null)}>
      <EmbedPDF key={gutter} engine={pdf.engine} plugins={plugins}>
        {({ activeDocumentId }) => activeDocumentId && (
          <DocumentContent documentId={activeDocumentId}>
            {({ isLoaded, isError }) => isError ? <div className="view-msg">PDF 打开失败</div>
              : isLoaded ? <Pages {...props} page={page} gutter={gutter} hovered={hovered} onHover={setHovered} /> : <div className="view-msg">正在打开 PDF…</div>}
          </DocumentContent>
        )}
      </EmbedPDF>
    </div>
  );
}
