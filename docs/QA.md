# API 边读边问

问答使用 `ask` 任务，可通过模型设置接口配置，也可在 `llm.yaml` 中显式加入：

```yaml
tasks:
  ask: {provider: main, model: YOUR_MODEL, prompt: ask}
```

目前使用结构化 API 输出。提问后保存用户原文并排队，由常驻服务优先执行按需任务；只查历史、状态或重复提交相同请求不会调用模型。命令行可用 `litgleaner run --stage ask` 处理待答问题。

| 接口 | 用途 |
|---|---|
| `POST /api/papers/{paper_id}/qa` | 提交问题，返回 202 和问题记录、状态；接收 `text`、`request_id`，可带 `selection` 和零基 `page` |
| `GET /api/papers/{paper_id}/qa` | 返回最近 50 个问题及答案，按时间、ID 正序；`limit` 可设 1–200 |
| `GET /api/papers/{paper_id}/qa/{question_id}` | 查询单题状态和答案 |
| `POST /api/papers/{paper_id}/qa/{question_id}/annotation` | 答案完成后保存关联批注：`body` 只存用户提问，`quote` 保留核验过的作者原文，`qa_question_id` 关联完整模型答案 |

接口沿用启动会话、Host/Origin 和 CSRF 保护。调用方为每次新提问生成唯一 `request_id`，网络重试时沿用。相同论文、相同请求 ID 和内容返回原问题；同一请求 ID 用于不同内容时拒绝。问题和答案只追加，已保存答案不会因任务重复执行而重新计费。

回答由 `statements` 组成，每项包含 `text`、`citations` 和服务端算出的 `inference`。引用包含本篇 `paper_id`、零基 `page`、原文 `quote`，必须同时通过页码、解析原文和已发送上下文核验。伪造或跨论文引用会丢弃；没有有效引用的陈述标记 `inference=true`。引用核验只能证明引文存在于解析文字，仍需以 PDF 原文核对关键数字和公式。

关联批注保留问答的结构化来源：通过 `qa_question_id` 查询本篇单题接口即可读完整回答。模型回答继续只按模型来源进入检索，不复制进“用户笔记”的索引。用户编辑批注正文不会改写原问题或模型答案。

状态包括 `queued`、`retrying`、`done`、`failed`、`configuration_required` 和 `budget_exceeded`。新问题缺模型时返回稳定的 `model_configuration_required`，不写入问题或任务。预算耗尽时保留 pending，恢复预算后继续。模型费用按论文记录，提供方已报告用量的失败响应也计费。

排队任务保留 PDF 内容、切分版本和阅读语言指纹；执行前发现变化时要求重新提问，避免用变更后的输入替用户继续收费。已有历史答案照常可读，不会自动重做。

上下文目前采用有界的本地词法检索，支持中文短词，选区和所在章节优先；系统提示、问题、画像和原文合计按估算 4000 token 控制。用户选区须确实存在于指定页。问题、画像和论文原文全部作为不可信数据隔离。问答分别按用户和模型来源进入全局 FTS 检索。

本段提供单问 API。阅读页问答组件仍需改接队列及历史接口；自动多轮上下文、FTS 与本地向量混合排序、论文间已核实关系和个人记忆的上下文接入、本机 agent 通道随后续模块完成。

2026-11-11 本地验收：后端全量 1579 项通过，覆盖率 94.33%；Ruff、格式、Mypy 和依赖契约通过。前端 28 项测试、类型检查与构建通过。独立 Python 复审无剩余阻断。测试使用模拟模型及隔离数据库，未使用真实模型、真实钥匙串或外部网络。
