<p align="center"><img src="docs/assets/logo.svg" width="128" alt="拾穗 LitGleaner 图标"></p>

<h1 align="center">拾穗 LitGleaner</h1>

<p align="center">每天在文献的麦田里，拾起值得读的那几穗。</p>

每天从多个信号源发现文献，按研究主题筛选和评分，把值得读的归档并推送。设计依据见 [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md)。

> 当前阶段：流程、存储、解析器，以及 9 个信号源和 8 个全文来源已经是真实实现；大模型已支持 Anthropic Messages、OpenAI Chat Completions / Responses、Gemini 四种协议（默认关闭，配置见 `config/llm.yaml`）。推送已支持飞书卡片，卡片上可以一键加入本机 Zotero（默认关闭，见 [docs/CHANNELS.md](docs/CHANNELS.md)），这部分只做过离线测试。`config/` 默认是演示配置，真实来源都已列出但默认关闭，启用方法见 [docs/SOURCES.md](docs/SOURCES.md)。

## 快速开始

```bash
uv sync
uv run litgleaner db upgrade
uv run litgleaner topic activate demo-topic
uv run litgleaner run
cat data/demo/outbox.jsonl     # 查看推送内容
ls -R data/demo/archive        # 查看 JSON 归档
```

领读器（需要 Node.js 和 pnpm 构建前端）：

```bash
(cd web && pnpm install && pnpm build)
uv run litgleaner serve        # 只监听本机，打印启动链接并打开浏览器；同时每 5 分钟运行一次流程
```

## 常用操作

| 想做什么 | 怎么做 |
|---|---|
| 检查配置和密钥 | `litgleaner doctor` |
| 增减信号源 | 改 `config/sources.yaml`，各来源的能力和限制见 [docs/SOURCES.md](docs/SOURCES.md) |
| 调整全文来源和顺序 | 改 `config/app.yaml` 的 `fulltext` |
| 真实网络联调 | `uv run python scripts/probe_sources.py`（不写数据库） |
| 切换研究主题 | 新建 `config/topics/<id>.yaml`，执行 `topic activate`、`topic pause` |
| 让新主题处理历史论文 | `topic backfill <id>` 预览，加 `--execute` 执行 |
| 改权重或等级线 | 改 `config/rubrics/default.json`，执行 `rubric rescore`（不花钱） |
| 改评价维度的定义 | 改同一文件，执行 `rubric reassess <id>` 预览，加 `--execute` 执行（花钱） |
| 查看历史结果是否过期 | `stale` 零费用核对；`--format json` 输出全部历史及无法完整核验的原因，见 [docs/STALE.md](docs/STALE.md) |
| 查询文库论文的事实关系 | 本地只读接口 `/api/papers/{id}/memory-facts`，见 [docs/PAPER-MEMORY.md](docs/PAPER-MEMORY.md) |
| 查看文库共同引用的重要前作 | 只读 `/api/memory/important-works`，返回计票门槛和逐篇原始引文，见 [docs/PAPER-MEMORY.md](docs/PAPER-MEMORY.md#文库重要前作) |
| 计算本地论文向量 | `memory reembed` 预览，`memory reembed --execute` 显式重算，见 [docs/LOCAL-EMBEDDINGS.md](docs/LOCAL-EMBEDDINGS.md) |
| 核实论文关系与历史回填 | 首次入库按需核实；`memory backfill` 预览、`--execute --plan-hash` 确认；双侧原文及反馈见 [docs/PAPER-RELATIONS.md](docs/PAPER-RELATIONS.md) |
| 查看文库相关论文候选 | 只读 `/api/papers/{id}/memory-candidates`；权重和阈值在 `memory.yaml` 配置，见 [docs/MEMORY-CANDIDATES.md](docs/MEMORY-CANDIDATES.md) |
| 结合论文与个人记忆提问 | 问答 API 冻结原文、已核实关系和用户笔记，按 FTS 与就绪本地向量召回，逐条核对跨论文出处，见 [docs/QA.md](docs/QA.md) |
| 增减推送渠道和时间 | 改 `config/channels.yaml` |
| 限制模型花费 | `config/llm.yaml` 配全局及每任务每日限额；任务用完后推迟，见 [docs/MODEL-SETUP.md](docs/MODEL-SETUP.md) |
| 飞书卡片推送、点按钮加入 Zotero | 配置渠道及 `zotero authorize` 后，`serve` 自动监听按钮，见 [docs/CHANNELS.md](docs/CHANNELS.md) |
| 手动把一篇论文加入 Zotero | `zotero add <DOI 或论文编号>` |
| 查看和重试失败任务 | `jobs list`、`jobs retry` |
| 处理发送结果未知的推送 | `delivery list`，确认后执行 `delivery resolve <digest> <渠道> --sent` 或 `--resend` |
| 重新生成归档 | `archive rebuild` |
| 导出课题对比表与引用 | `export table --topic <id>`、`export refs --topic <id>`，见 [docs/TOPIC-EXPORT.md](docs/TOPIC-EXPORT.md) |
| 整理已读论文综述提纲 | `export outline --topic <id>` 只预览；显式确认后按需生成，见 [docs/OUTLINE.md](docs/OUTLINE.md) |
| 备份数据库 | `serve` 每天自动备份；`backup` 手动备份，`db upgrade` 迁移前备份；默认保留 14 份，见 [docs/BACKUP.md](docs/BACKUP.md) |
| 统一翻译里的术语译名 | 写 `config/topics/<主题 id>.glossary.yaml`：`terms: [{en: self-attention, zh: 自注意力}]`，`en` 可以是多个写法的列表 |
| 阅读、批注、管理待读 | `serve` 后在浏览器里操作；链接每次启动都会变，只能在本机打开 |
| 恢复演练 | `restore --check <快照>`：在临时目录还原并校验，不改动正在用的数据库 |

## 解析器

| 适配器 | 说明 |
|---|---|
| `mineru_cloud` | 默认推荐。需要 `MINERU_API_TOKEN`（`source .env.example` 从钥匙串注入） |
| `pdf_text` | 不耗额度，英文论文效果接近 MinerU，中文期刊明显较差 |

对比数据见 [docs/experiments/2026-11-05-parser-comparison.md](docs/experiments/2026-11-05-parser-comparison.md)。

## 开发检查

```bash
uv run pytest --cov=litgleaner
uv run ruff check src tests && uv run ruff format --check src tests
uv run mypy
uv run lint-imports
```

## 许可证

[MIT](LICENSE)
