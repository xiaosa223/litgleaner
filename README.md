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
| 增减推送渠道和时间 | 改 `config/channels.yaml` |
| 飞书卡片推送、点按钮加入 Zotero | 见 [docs/CHANNELS.md](docs/CHANNELS.md)：`zotero authorize`、`feishu listen` |
| 手动把一篇论文加入 Zotero | `zotero add <DOI 或论文编号>` |
| 查看和重试失败任务 | `jobs list`、`jobs retry` |
| 处理发送结果未知的推送 | `delivery list`，确认后执行 `delivery resolve <digest> <渠道> --sent` 或 `--resend` |
| 重新生成归档 | `archive rebuild` |
| 备份数据库 | `backup`（保留份数见 `app.yaml` 的 `backup`）；`db upgrade` 迁移前会自动备份 |
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
