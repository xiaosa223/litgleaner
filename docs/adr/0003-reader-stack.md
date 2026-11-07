# ADR 0003：领读器技术栈与常驻服务的分层

- 状态：已接受
- 依据：ARCHITECTURE.md 第 4、12、15.5 节

## 背景

第 15.5 节规定了常驻服务的职责和本地安全要求，第 4 节规定了 `service.py`、`server/`、`web/` 的位置，但没有选定 HTTP 框架和前端技术栈。领读器的就地释义和翻译要用课题术语表（第 7.1 节），而由模型起草术语表的 `topic draft` 排在 ROADMAP 第 7 步。

## 决定

1. **后端：FastAPI + uvicorn。** 请求和响应用 Pydantic 模型校验（项目已在用 Pydantic）；段落翻译用 SSE 流式返回。`server/` 只做协议适配：鉴权、校验、序列化，查询和操作都调用 `application` 中的函数。
2. **前端：Vite + React + TypeScript**，放在 `web/`，原版视图用 `pdfjs-dist`。构建产物复制到 `src/litgleaner/server/static/` 随 Python 包发布，开发时由 Vite 开发服务器代理到本地接口。
3. **入口层的依赖顺序**（import-linter）：`cli → cli_library → service → server → cli_shared → bootstrap`。`service` 组合 `server`、调度和渠道监听，`litgleaner serve` 命令启动它。
4. **数据库对象要能跨线程使用。** 常驻服务里 HTTP 请求在线程池中处理，调度在后台线程运行。`Database.atomic()` 的当前事务按线程隔离；写入仍由运行锁和短事务保证顺序（第 15.1 节）。
5. **术语表在第 5 步先支持手写**：读取 `config/topics/<topic-id>.glossary.yaml`（可以不存在，视为空表），翻译和释义注入术语，译文指纹计入 `glossary_hash`。由模型起草术语表仍在第 7 步实现。

## 后果

- 新增运行时依赖 `fastapi`、`uvicorn`；开发需要 Node.js 和 pnpm 构建前端，最终用户不需要。
- 发布前要先构建前端；`litgleaner serve` 找不到构建产物时只提供接口，并提示先构建。
- 新增阅读相关的表（`reading_state`、`annotation` 等）按第 5 节的键设计，通过 Alembic 迁移加入。
