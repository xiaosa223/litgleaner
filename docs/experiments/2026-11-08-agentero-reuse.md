# Agentero 复用调研（2026-11-08）

对象：[poco-ai/Agentero](https://github.com/poco-ai/Agentero)，MIT，提交 `11219aa`（v0.15.2，2026-11-07）。本地参考副本在 `../Agentero`（拾穗仓库外，不进 git）。

方法：只读文件，没有安装依赖、构建或运行。行数是调研时用 `wc -l` 数的。标 **推断** 的是没有在代码里直接看到的判断。

## 1. 总体情况

| | Agentero | 拾穗 `web/` |
|---|---|---|
| 形态 | Tauri 2 桌面应用，Rust Host 负责文件、解析、翻译服务、agent | 浏览器网页，Python 本地 HTTP 接口 |
| 前端栈 | React 19、Tailwind 4、shadcn/ui、zustand、react-i18next、motion | React 19、普通 CSS |
| PDF 引擎 | EmbedPDF（PDFium wasm）及 13 个插件，另有 7 个本地补丁 | pdfjs-dist |
| 阅读器规模 | `src/components/viewer/pdf/` 约 2.5 万行，依赖的 `src/lib/pdf/` 约 2.5 万行 | 全部 `web/src` 约 2.1 千行 |
| 文件读写 | 全部经 `src/lib/vault/fs.ts`（`@tauri-apps/plugin-fs`），非 Tauri 环境直接抛错 | 经本地接口 |

结论：**不能整块拷贝**。耦合集中在三处：Tauri 文件读写、Tailwind + shadcn 的界面、全局 zustand store。纯逻辑模块可以直接拷，界面组件多半要改写。

## 2. PDF 阅读器

| 模块 | 关键文件（行数） | 耦合 | 复用方式 |
|---|---|---|---|
| 引擎和插件装配 | `src/lib/pdf/engine.ts`、`engine-provider.tsx`（78）、`pdf-viewer.tsx` 的插件段 | 只有 `system-font-fallback.ts` 调 Tauri 取系统中文字体；Vite 需要 `assetsInclude: wasm`、`worker.format: es` | 直接拷，补一个提供中文后备字体的接口 |
| EmbedPDF 补丁 | `patches/` 7 个，经 pnpm `patchedDependencies` 生效 | 文档称 EmbedPDF 2.15.1 仍需要 | 换引擎就一起带上 |
| 页面图层和视口 | `layers/page-layers.tsx`（1805）、视口缩放平移若干 | dockview 工作区 | 从 EmbedPDF 官方 React 示例起步更简单 |
| 划词菜单、高亮颜色 | `cards/selection-menu`（319）、`selection-card`（440）、`use-pdf-text-selection`（464） | Tailwind、shadcn | 借设计，或换栈后改造 |
| 高亮与批注的存储 | `use-pdf-highlights`（582）、`lib/pdf/highlight/*`、`use-pdf-marks-io`（343） | Vault 文件读写、文件监听 | 存储逻辑可拷，文件读写换成我们的接口；页边批注栏借设计 |
| 目录 | `use-pdf-outline`（84）、`outline-tree`（59） | 无（只读部分） | 直接拷 |
| 搜索 | `use-pdf-find`（123）、`pdf-find-bar`（128） | 无 | 直接拷 |
| 引用和交叉引用悬停预览 | `use-pdf-citations`（513）、`use-pdf-crossref-preview`（715） | 参考文献缓存、worker | 借设计（效果好，但耦合重） |
| 划词提问 | `use-selection-ask`、`selection-chat-popover`、`ask-popover` | agent 调用栈 | 借设计 |
| 图表面板、版面分析 | `figures-panel`（714）、`use-pdf-layout-run`（468） | 浏览器内 ONNX 模型、Host 任务 | 不用，我们已有 MinerU 版面 |

值得借的界面设计：划词工具条（颜色点向左展开、复制原地变成对勾）、页边批注栏带连线、引用悬停预览（找不到元数据时裁出参考文献条目图片）、用 `mix-blend-multiply` 叠加纸张色调、双指缩放时先做 CSS 变换预览，松手后再真正重渲染。

## 3. 全文翻译

两条路径都基本在前端完成：

1. **覆盖层**：在 PDF 页面上按版面块画一层纸色底的 HTML 译文，按 DOM 测量自动调字号。
2. **字符级重排**：从 PDFium 读出每个字形，删除原文、按真实字宽排版译文、保留矢量公式，用 pdf-lib 写出真正的译文 PDF，支持“译文版”和“原文译文交替页”导出。文档写明生产环境不依赖 Python，也不含 BabelDOC 代码（BabelDOC 是 AGPL，只在仓库外做对照实验）。

| 模块 | 文件（行数） | 耦合 | 复用方式 |
|---|---|---|---|
| 占位符保护 | `src/lib/translate/mask.ts`（109）、`preserved-links.ts`（147） | 无 | 直接拷，或移植到 Python |
| 分句与句对 | `src/lib/pdf/layout/layout-sentences.ts`（364） | 无 | 同上 |
| 译前归一化、跨页跨栏段落合并 | `layout-translate-source.ts`（263） | 无 | 同上 |
| 分批、`[[n]]` 编号解析、重试 | `layout-translate.ts`（1187，其中 `buildTranslateBatches`、`parseNumberedTranslation`）、`layout-translate-reliable.ts`（150） | i18n、文件读写 | 纯函数部分直接拷 |
| 提示词规则 | `src/lib/translate/prompt.ts`（285） | 无 | 直接拷规则 |
| 双栏滚动同步 | `src/lib/pdf/scroll-sync.ts`（514） | 不依赖框架 | 拷过来适配所用的引擎 |
| 覆盖层排版 | `layers/layout-translate-overlay.tsx`（949）、`layout-translate-dom-fit.ts` | Tailwind、EmbedPDF | 借设计 |
| 译文缓存设计 | `{paper}/source/layout-translate.json`（schema 2） | 文件读写 | 借设计；我们的缓存在 SQLite（ADR 0004） |
| 字符级重排与导出 | `src/lib/pdf/reflow/extract.ts`（672）、`layout.ts`（391）、`export.ts`（378）、`preview.ts`（503）、`cache.ts`（608） | **必须用打过补丁的 EmbedPDF 引擎**（逐字形读取、按区域删除文字），pdf.js 做不到 | 只有换成 EmbedPDF 才能复用 |
| 视图钩子和译文面板 | `use-pdf-layout-translate.ts`（957）、`pdf-translation-viewer-inner.tsx`（565） | zustand、i18n、EmbedPDF | 改写 |

规则摘要（可以直接用）：
- 公式、LaTeX 命令、URL、DOI、方括号引用、作者-年份引用（含全角括号和“等人”）、图表节公式的交叉引用、Markdown 链接，先换成 `⟦n⟧`，回填时检查缺失、重复、未知；仍失败就保留原文，不用部分译文覆盖。
- 拉丁 `.!?` 后面要有空白才切句，中文句号单独切；`et al.`、`Fig.`、`e.g.` 和小数不切。
- 每批不超过 4500 字符（约一页双栏正文），每句一个 `[[n]]` 编号，并发 2。每个段落一完成就立刻显示，不等整页。

**版面来源**：Agentero 已经支持把 MinerU 当作版面来源（`src-tauri/.../layout/hosted/mineru.rs:284-409`）：把 `content_list.json` 的类型映射到自己的标签，把 0–1000 坐标按 `middle.json` 的页面尺寸换算，置信度记为 1.0。我们可以同样用 MinerU 的块，不引入浏览器内的 ONNX 模型（PP-DocLayoutV3，约 130 MB，许可证仓库里没写）。

我们这边要补的：
- `NormalizedDocument` 要带页面尺寸。
- 图题、表题要成为独立的块；现在是挂在图表块上的字段。
- 页眉、页脚现在被丢弃，翻译时也不需要，保持不变。
- **风险**：MinerU 识别出的文字和 PDF 文字层不完全一致。要让句子能对准原文字形，块内文字应从 PDF 文字层按 bbox 取，MinerU 只提供块的位置和类型。

**导出**：服务端用 PyMuPDF 做最省事，但 PyMuPDF 是 AGPL，不适合随 MIT 安装包分发。保版式导出最好复用 Agentero 的 TS 重排（MIT），而这又要求换成 EmbedPDF。字体用 BabelDOC-Assets 里的 Noto Serif 和 Source Han Serif（OFL 1.1），按固定提交和 SHA-256 首次下载，不进仓库。Agentero 因为子集化会丢字形，完整嵌入中文字体，导出的文件约 20 MB。

已知限制（摘自 Agentero 文档）：
- 效果不等同 BabelDOC：图内标签、旋转文字、复杂多级表格仍然失败，作者名会被误译；
- 不支持扫描件；
- 重排路径的对齐单位是段落，不是句子；
- 双栏同步按页面比例，不按句子；
- 首批翻译要等版面分析和字形提取完成。

## 4. 本机 agent（ACP）

见 [ADR 0005](../adr/0005-local-agent-first.md)。可以借鉴的是 Agentero `docs/backend/agent.md` 里的避坑经验：
- 冷启动超时：初始化 30 秒，载入会话 60 秒。
- macOS 从图形界面启动时，工作目录是 `/`，会触发无关的系统权限弹窗；要先切到指定目录再启动。
- 要合并登录 shell 的环境变量，才能读到用户在 `.zshrc` 里设置的 key 和 PATH。
- Windows：`.cmd` 包装、带括号路径的转义、只能结束直接子进程（进程残留）。
- DeepSeek Harness 的 ACP 模式需要 Node 22.19+ 或 24.2+，读用户自己的 `DEEPSEEK_API_KEY`。
- 各家 agent 向用户提问的方式不统一；我们拒绝所有权限请求、只把 agent 当模型用，可以避开大部分差异。

## 5. 许可

- Agentero 是 MIT（Copyright (c) 2026 poco-ai）。拷贝的源码要保留版权声明，在仓库根目录建 `THIRD_PARTY.md` 逐项登记来源文件和提交号。
- **推断，未核实**：EmbedPDF 是 MIT；PDFium 是 BSD-3 / Apache-2.0；pdf-lib、motion 是 MIT。
- `@embedpdf/ai`、`plugin-ai-manager`、`plugin-layout-analysis` 都是 0.0.1 版，许可证和模型权重的许可证未核实。我们不用版面分析插件，可以不引入它们。
- 字体是 OFL 1.1，只下载不入库。
- 不拷贝任何 BabelDOC 代码（AGPL）。

## 6. 取舍

| 方案 | 做法 | 好处 | 代价 |
|---|---|---|---|
| A：保留 pdf.js，只借设计和纯逻辑 | 拷占位符保护、分句、归一化、分批、滚动同步；覆盖层和双栏自己用 pdf.js 写 | 依赖最少，现有代码不动 | 保版式的译文 PDF 导出要另找方案（PyMuPDF 是 AGPL）；以后每个功能都要自己写 |
| B：换成 EmbedPDF，界面用我们自己的样式 | 引擎、插件、补丁、搜索、目录、高亮存储直接拷；界面按 Agentero 的设计用现有样式改写 | 以后能复用 Agentero 的字符级重排和译文 PDF 导出；引擎能力（逐字形几何、批注写回 PDF、分块渲染）一步到位 | 带上 7 个补丁，升级 EmbedPDF 时要跟着改；界面仍要改写 |
| C：前端整体换成 Agentero 的栈 | 引入 Tailwind、shadcn、zustand，界面组件按原样改造，只把 Tauri 文件读写换成本地接口 | 能拷的最多，开发最快，后续可以持续同步上游改进 | 依赖变重；和上游耦合深，上游重构时同步成本高；不需要的功能（i18n 等）要一路剥离 |

**建议：B，并在界面层按需引入 Tailwind 和 shadcn**（即 B 往 C 靠一步）。理由：
- 全文翻译是 P0。保版式导出只有在 EmbedPDF 上才能复用 Agentero 的现成实现，否则要自己从头写，或者引入 AGPL 依赖。
- 我们的前端现在只有 2.1 千行，现在换引擎最便宜。
- 引入 Tailwind 和 shadcn 后，划词菜单、批注栏这类组件能少改很多。zustand 和 i18n 不跟：界面只有中文，状态按现有方式组织。

这是 ADR 0003（前端用 pdfjs-dist）的修订，记录在 [ADR 0006](../adr/0006-platform-and-reader-reuse.md)。
