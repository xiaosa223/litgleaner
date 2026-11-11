# 已读论文综述骨架

对当前课题画像下相关且已标记为 `read` 的论文生成短提纲。使用独立 `outline` 模型任务，先预览范围和费用，再显式确认排队。不会因为新增论文、改配置或打开页面自动生成。

## CLI

```bash
uv run litgleaner export outline --topic example-topic
uv run litgleaner export outline --topic example-topic --execute --plan <plan_hash>
uv run litgleaner run --stage outline
uv run litgleaner export outline --topic example-topic --result <result_id> > outline.md
```

默认返回 JSON 预览，包括实际输入和预算遗漏编号、token 预算、缓存编号及费用估计。确认返回任务状态；由常驻服务按需队列或 `run --stage outline` 执行。执行完成后再次预览，从 `cached_outline_id` 获取结果编号，也可从任务状态 API 读取。下载只返回已有结果的 Markdown，不触发生成。`--execute` 必须与 `--plan` 同用，与 `--result` 互斥；旧预览输入变化时要求重新确认。

`estimated_usd` 使用本任务历史单次均值，无历史时为 `null`，空输入或命中缓存时为零。实际费用按任务 scope 汇总，包含有已知用量的失败响应。预算不足保持待处理，临时错误沿用队列退避，重试范围不扩张。

## HTTP

全部需要本机会话；POST 还需要 `X-CSRF-Token`，请求体拒绝额外字段。

| 方法与路径 | 用途 |
|---|---|
| `POST /api/topics/{topic_id}/outline/preview`，`{}` 或省略 body | 只读预览，不联网、不读 PDF、不写数据库、不调用模型 |
| `POST /api/topics/{topic_id}/outline`，`{"plan_hash":"..."}` | 重新核对指纹后幂等排队，返回 202 |
| `GET /api/topics/{topic_id}/outline` | 当前匹配历史、过期原因、新增已读论文和未分组编号 |
| `GET /api/topics/{topic_id}/outline/jobs/{job_key}` | 任务状态、结果、费用和错误 |
| `GET /api/topics/{topic_id}/outline/history?limit=20` | 按时间和编号倒序读取，最多 100 条 |
| `GET /api/topics/{topic_id}/outline/results/{outline_id}` | 按编号读取本课题的冻结结果 |
| `GET /api/topics/{topic_id}/exports/outline?result_id=...` | 下载指定已有结果 |

下载固定文件名 `litgleaner-outline.md`，带 `Cache-Control: no-store` 和 `X-Content-Type-Options: nosniff`，不接受本机输出路径。跨课题任务和结果不可读取。缺少模型配置时仍可读历史和预览，确认生成返回结构化 409；CLI 返回友好说明。

## 输入、编号与证据

范围复用课题导出的六次一致查询，再限定 `reading_state.status=read`。不受首页最近推荐、已推送或待读数量影响；暂停课题仍允许显式操作。没有可发送论文时拒绝确认。

`app.yaml` 的 `outline.token_budget` 默认 12000，严格接受 512–64000 的整数，包括完整系统提示、数据隔离包装与论文 JSON。按论文编号稳定顺序逐篇纳入；画像本身超过预算时拒绝预览。每篇给标题前 1000 字、已有方法及数据集的文字和引文各前 500 字、最多八个数据集和目标关联编号，不补取全文或重新提取。遗漏论文保持可见。

模型最多给 12 个短组名和短说明、对应目标与论文编号，及最多 12 条有论文编号依据的覆盖缺口候选；材料不足允许全部留空。经过 JSON Schema 后，代码再清理未发送编号、重复归组、无效目标、空分组和无依据候选；不为填满分组再次调用模型。全部未归组论文单列。

覆盖缺口候选只限本次实际发送的已读集合。每个支撑论文必须有当前版本、完整发送、已核验的方法和所有数据集证据；截断、遗漏、过期或未核验内容都不能支撑候选。候选含任一不合格编号则整条丢弃。模型判断保留明确标签，缺少提取内容不作为整个领域缺少研究的证据。

## 附件、缓存与历史

每组对比表由代码复用 [课题导出](TOPIC-EXPORT.md) 的事实列和评分规则；正式引用复用来源元数据渲染器，缺失或歧义标识符单列核对，格式警告保留。模型不编写引用或指标结果。表格、引用和模型文本按各自来源呈现，Markdown 文本转义，引用使用不会被内容突破的代码围栏。

迁移 `0017` 仅新增 `topic_outline`。数据库禁止修改、删除和替换已有记录，课题、画像、输入与生产者联合唯一。保存输入和确认范围、完整来源指纹、语言、预算、模型、费用 scope、分组与候选，以及代码生成的表格和引用附件。历史下载使用生成时附件，后续来源更新不会改写它。

确认与执行分别检查固定输入、当前已读及课题归属、模型/提示词、阅读语言、预算和完整来源。队列 JSON 与附件统一序列化，再核对实际正文、附件、候选资格及完整消息预算。来源变化的旧任务停止执行，要求重新预览；确认后新读论文不扩张旧输入。

读取优先选择当前配置且已发送来源仍匹配的最近历史。切回旧配置可复用对应缓存；无匹配时保留可读历史并标记过期。新增已读论文单独提示，不自动收费。课题页查看、确认和下载入口尚未接线；当前验证使用离线模型和本地数据库，未调用真实来源、付费模型或钥匙串。

本段全量 3199 项测试通过，覆盖率 95.10%；全局 Ruff、420 个文件格式检查、268 个源文件 Mypy 和三项依赖契约通过。独立审查覆盖领域、数据库、模型配置、队列、API/CLI、输入截断和冻结附件边界。
