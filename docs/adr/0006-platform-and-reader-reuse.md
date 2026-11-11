# ADR 0006：网页优先、多平台路线，领读器借用 Agentero

- 状态：已接受（2026-11-08，按推荐方案）
- 被修订：第 3 条（界面只有中文）由 [ADR 0009](0009-survey-read-retain.md) 修订
- 修订：ADR 0003 第 2 条（前端用 pdfjs-dist）；ARCHITECTURE.md 第 12.2、12.3、15.5 节；REQUIREMENTS.md R2、R25
- 依据：[Agentero 复用调研](../experiments/2026-11-08-agentero-reuse.md)

## 背景

- 用户决定：先做网页版；学生大多用 Windows，Windows 一等支持，以后兼容 macOS、Linux 和手机；全文翻译是刚需；领读器尽量借用 Agentero（MIT），不重复造轮子。
- 调研结论：Agentero 的阅读器约 5 万行，绑定 Tauri 文件读写、Tailwind + shadcn、zustand、i18n，不能整块拷贝；纯逻辑模块可以直接拷。保版式全文翻译的字符级重排和译文 PDF 导出在前端完成，但依赖打过补丁的 EmbedPDF 引擎，pdf.js 做不到。
- 我们的前端现在约 2.1 千行，换引擎的成本最低就是现在。

## 决定

1. **网页优先，一套前端走所有平台。** 浏览器、可选的桌面外壳（系统 WebView）、以后的手机端都打开同一个本地服务的网页。前端不依赖桌面外壳的专有能力。Windows 的路径、编码、子进程规则写进 ARCHITECTURE 第 15.5 节，CI 在 Windows 上跑测试。
2. **PDF 引擎从 pdfjs-dist 换成 EmbedPDF**（PDFium wasm），连同 Agentero 的插件配置和 7 个补丁。从 Agentero 直接拷：引擎装配、搜索、目录、高亮存储逻辑（把文件读写换成本地接口）。中文后备字体由本地接口提供。
3. **界面层引入 Tailwind 和 shadcn/ui**，以便少改写 Agentero 的划词菜单、批注栏等组件。不引入 zustand 和 i18n：界面只有中文，状态管理沿用现有方式。
4. **全文翻译**：
   - **版面来自 MinerU 的块**，不引入浏览器内的版面模型；块内文字从 PDF 文字层按 bbox 读取，使句子能对准原文字形。`NormalizedDocument` 增加页面尺寸，图题、表题成为独立的块。
   - **翻译规则直接用 Agentero 的**：`⟦n⟧` 占位符保护、分句、跨页跨栏段落合并、每句 `[[n]]` 编号分批。按 ADR 0004 的思路在后端校验句数和术语表，校验通过才写缓存。
   - **显示**：覆盖原文的保版式视图、左右双栏对照、按页翻译、按段翻译；随时切回原文。
   - **导出**：译文 PDF 和原文译文交替页 PDF，复用 Agentero 的字符级重排（MIT），不用 BabelDOC（AGPL）和 PyMuPDF（AGPL）。原第 12.3 节通过 `Translator` 调 BabelDOC 的方案撤销。
5. **来源登记**：仓库根目录建 `THIRD_PARTY.md`，逐项登记从 Agentero 拷来的文件、上游路径、提交号和许可声明。参考副本放在仓库外的 `../Agentero`。

## 实施前核实

- EmbedPDF 各包的许可证在引入前用 `npm view` 逐个核实；不引入版面分析相关的 `@embedpdf/ai`、`plugin-ai-manager`、`plugin-layout-analysis`。
- 备选方案（保留 pdf.js、只借设计和纯逻辑）未采用，比较见调研文档第 6 节。

## 后果

- `web/` 的阅读页要重写到新引擎上。已实现的功能（段落叠加、选中工具条、高亮笔记、阅读进度、段落翻译）要在新引擎上重新走查。
- 后端要补：带页面尺寸和独立图表题块的版面接口；按句分批的翻译接口；中文后备字体接口；导出用的字体下载（OFL，按固定提交和 SHA-256 校验）。
- 撤销 BabelDOC 方案：ARCHITECTURE 第 1 节的变化点表、第 4 节的目录和 `Translator` 接口、第 5 节 `translation` 的说明、第 15.3 节的数据去向、第 17 节的变更场景已同步修改。
- 补丁随 EmbedPDF 升级要跟着改，升级前对照 Agentero 上游的做法。
