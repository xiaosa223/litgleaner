# 论文工厂

每天从多个信号源发现文献，按研究主题筛选和评分，把值得读的归档并推送。设计依据见 [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md)。

> 当前处于“行走骨架”阶段：流程和存储都是真实的，但信号源、全文、解析、大模型和推送都是假适配器，`config/` 里的内容只用于演示。

## 快速开始

```bash
uv sync
uv run litfactory db upgrade
uv run litfactory topic activate demo-topic
uv run litfactory run
cat data/demo/outbox.jsonl     # 查看推送内容
ls -R data/demo/archive        # 查看 JSON 归档
```

## 常用操作

| 想做什么 | 怎么做 |
|---|---|
| 检查配置和密钥 | `litfactory doctor` |
| 增减信号源 | 改 `config/sources.yaml` |
| 切换研究主题 | 新建 `config/topics/<id>.yaml`，执行 `topic activate`、`topic pause` |
| 让新主题处理历史论文 | `topic backfill <id>` 预览，加 `--execute` 执行 |
| 改权重或等级线 | 改 `config/rubrics/default.json`，执行 `rubric rescore`（不花钱） |
| 改评价维度的定义 | 改同一文件，执行 `rubric reassess <id>` 预览，加 `--execute` 执行（花钱） |
| 增减推送渠道和时间 | 改 `config/channels.yaml` |
| 查看和重试失败任务 | `jobs list`、`jobs retry` |
| 处理发送结果未知的推送 | `delivery list`，确认后执行 `delivery resolve <digest> <渠道> --sent` 或 `--resend` |
| 重新生成归档 | `archive rebuild` |

## 解析器

| 适配器 | 说明 |
|---|---|
| `mineru_cloud` | 默认推荐。需要 `MINERU_API_TOKEN`（`source .env.example` 从钥匙串注入） |
| `pdf_text` | 不耗额度，英文论文效果接近 MinerU，中文期刊明显较差 |

对比数据见 [docs/experiments/2026-11-05-parser-comparison.md](docs/experiments/2026-11-05-parser-comparison.md)。

## 开发检查

```bash
uv run pytest --cov=litfactory
uv run ruff check src tests && uv run ruff format --check src tests
uv run mypy
uv run lint-imports
```
