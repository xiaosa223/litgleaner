# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

拾穗 LitGleaner（包名 `litgleaner`）：研究者写下研究背景、内容和目标，系统每天从多个学术来源检索文献，筛选、评分、提炼后推送到飞书卡片，并在自带的领读器里帮助研究者阅读、翻译、沉淀笔记；本机 Zotero 是可选同步。发布形态是桌面安装包加常驻服务。项目会分发给其他研究者独立运行：用户功能必须由本项目自己实现，不能依赖本机的 agent-autoloop（它只用于本机运维，见 `scripts/autoloop_run.py`）。

## 最高规则：一切增改服从架构设计

`docs/ARCHITECTURE.md` 是实现的唯一依据，`docs/adr/` 记录对它的修订。任何新增或修改（代码、配置结构、数据库表、CLI 命令、提示词、目录）都必须符合其中的决策。

文档分工：
- `ARCHITECTURE.md` 只写目标功能和设计，不写进度、日期、实测经过。
- `docs/ROADMAP.md` 写当前进度、实施顺序、代码与文档的已知差异、待实测项。
- `docs/experiments/` 写实测记录。

规则：
- **动手前**：先读 ARCHITECTURE.md 中与本次改动相关的章节（变化点见第 1 节，变更场景对照见第 17 节），确认改动落在设计规定的位置、只动设计说该动的文件。再查 ROADMAP.md 的“已知差异”，看这块代码是否本来就落后于文档。
- **发现冲突就停下讨论，不擅自偏离**。冲突包括：用户的需求与架构决策矛盾；按架构做法实现不了或代价明显过高；架构文档没有覆盖到这个新概念；现有代码已经和文档不一致。此时向用户说明：冲突点（引用章节号与原文）、可选方案及各自代价、你的推荐。用户拍板前不写偏离设计的代码；与冲突无关、已授权的部分可以继续做。
- **决策变了先改文档**：用户同意调整设计后，先更新 ARCHITECTURE.md 或在 `docs/adr/` 新增一条记录（背景、决策、后果），再改代码。不允许“代码先行、文档以后补”。
- **不绕过机械检查**：`import-linter` 契约、`tests/architecture/` 规则、契约测试失败时修实现；认为规则本身该改，同样属于架构冲突，先讨论。
- 文档中标为“故意不做”的东西（第 16 节：消息队列、PostgreSQL、向量库、动态插件、公开的网页服务、多数据库、切分器接口、MCP 写入工具）在触发条件出现前不引入；用户要求引入时先指出对应条款。
- 完成一项功能后更新 ROADMAP.md 的进度和已知差异，不往 ARCHITECTURE.md 里写进度。

## 常用命令

依赖用 `uv` 管理，Python 3.12+。

```bash
uv sync                                    # 安装依赖（含 dev 组）
uv run litgleaner db upgrade               # 跑 Alembic 迁移
uv run litgleaner doctor                   # 配置、密钥、假适配器自检
uv run litgleaner run                      # 跑全部阶段；--stage <name> 只跑一个阶段
uv run python scripts/probe_sources.py     # 真实网络联调信号源，不写数据库
(cd web && pnpm install && pnpm build)     # 构建领读器前端，产物在 src/litgleaner/server/static/
uv run litgleaner serve                    # 常驻服务：领读器 + 定时运行流程
(cd web && pnpm typecheck)                 # 前端类型检查
```

提交前的四项检查（与 README 一致）：

```bash
uv run pytest --cov=litgleaner             # 覆盖率门槛 80%（pyproject 中 fail_under）
uv run ruff check src tests && uv run ruff format --check src tests
uv run mypy                                # strict，只检查 src
uv run lint-imports                        # 依赖方向契约
```

单个测试与按层运行：

```bash
uv run pytest tests/unit/test_rubric.py::test_name
uv run pytest -m unit          # 标记 unit / contracts / integration / e2e 由 tests/conftest.py 按目录自动打上
uv run pytest tests/architecture
```

## 架构要点

### 分层与强制机制

```text
入口：cli → cli_library → service（组合 server、调度；飞书监听待并入）→ server → cli_shared；mcp_server（待实现）
  → bootstrap → adapters | infra → application → ports → domain
```

依赖只能向内，由 `pyproject.toml` 中的 import-linter 契约强制：适配器之间互相独立，适配器不能导入 `application` 和 `infra`（共享代码放 `adapters/common/`）。`tests/architecture/test_rules.py` 另外强制：

- `application/`、`ports/` 中不得出现任何具体来源名（正则含 mineru、zotero、arxiv、crossref 等）。流程代码写 `if source == "xxx"` 视为缺陷。
- 单文件 ≤ 400 行、单函数 ≤ 50 行（比全局约定更严，以本项目为准）。
- 库代码不得用 `print`，用 logging；CLI 输出走 typer。

新增 CLI 模块或适配器子包时，要同步更新 import-linter 的 `layers` / `independence` 列表——这本身是架构变更，按上面的规则处理。

### 适配器注册

`bootstrap.py` 是唯一把配置名映射到具体实现的地方，用显式字典（`SOURCES`、`FULLTEXT`、`PARSERS`、`LLMS`、`PUBLISHERS`、`LIBRARIES`、`RENDERERS`），不做动态插件加载。加一个适配器 = 适配器文件 + 注册表一行 + `config/*.yaml` 一段 + 通过 `tests/contracts/` 的统一契约测试（外部响应录制到 `tests/fixtures/<vendor>/`）。密钥只从环境变量读，配置里只写 `credentials_env` / `optional_env` 变量名，`BuildArgs.secret()` 会拒绝未声明的变量。

同一个 API 的元数据发现（`Source`）和全文获取（`FullTextProvider`）是两个独立适配器。HTTP 限速、错误分类、密钥脱敏、原始响应留档统一由 `adapters/common/http.py` 负责；第三方 PDF 一律走 `adapters/common/download.py`（SSRF、大小、文件头校验）；游标窗口用 `adapters/common/window.py`。适配器只抛 `domain/errors.py` 中的统一错误类型，重试和退避由调度负责。

### 事实与判断分离（核心原则）

- **事实层**全局一份：`paper`、`paper_alias`、`sighting`、`artifact`、`fulltext`、`extraction`。
- **判断层**按“主题 + 评价标准”分开存：`triage`（键含 `topic_hash`）、`assessment`（键含 `semantic_hash`）、`score`（键含 `policy_hash`）。
- 所有派生物带“输入哈希 + 生产者版本”指纹，配置变化时只算出失效范围，**任何配置修改都不能静默产生 LLM 费用**：改主题、维度、提示词只影响新论文，历史要显式 `topic backfill` / `rubric reassess --execute`；改权重和等级线用 `rubric rescore`（零费用）。
- SQLite 是唯一真相；JSON 归档由 `archive` 阶段导出，不手改、不反向写入。表结构只通过 Alembic 迁移（`infra/db/migrations/versions/`）改变。
- 领域模型不可变（frozen），`feedback` 等记录只追加。

### 流水线

`application/pipeline.py` 的 `STAGES` 元组定义阶段顺序：discover → triage → acquire → parse → extract → assess → score → archive → select → publish → library。每个阶段继承 `stages/base.py` 的 `Stage`，`plan()` 产出带幂等键的 `JobSpec`，`process()` 处理单个任务；调度、重试、`Deferred` 推迟、死信、失败隔离全在 pipeline，阶段内不处理。初筛通过才取全文，这是控制成本的主要手段。

### 变化点的约束

- **评价指标**：`config/rubrics/<id>.json` 是维度和权重的唯一来源，代码和提示词不写死维度；总分 `reading_value` 由 `domain/rubric.py` 纯函数算出，LLM 只给各维度等级和证据。
- **解析**：每个解析适配器必须转换成 `NormalizedDocument`（防腐层），下游切分、提取、评估不感知 MinerU 等格式；未知块类型保留文字并标 `degraded`，不静默丢弃。切分器 `domain/chunker.py` 是纯函数。
- **存储**：业务代码只用 `blob://` 地址和 `BlobStore` 接口，不出现文件路径。
- **发布**：Selector → 不可变 Digest → 可见性投影 → Renderer → Publisher → `delivery`。选文不感知渠道；公开内容走字段白名单，新字段默认不公开；结果未知的推送不自动重发。
- **LLM**：按任务在 `config/llm.yaml` 配模型和预算，提示词放 `prompts/`；论文内容视为不可信数据，输出必须过 JSON Schema 校验。

## 相关文档

- `docs/REQUIREMENTS.md`：用户需求与证据等级，架构取舍的依据。
- `docs/ARCHITECTURE.md`：目标设计，实现依据。
- `docs/ROADMAP.md`：进度、实施顺序、代码与文档的已知差异、待实测项。
- `docs/CHANNELS.md`：飞书推送与加入 Zotero 的配置步骤。
- `docs/SOURCES.md`：已接入信号源、全文来源的能力与限制。
- `docs/experiments/`：解析器对比等实测记录，修改解析或切分规则前先看。
- `docs/ops/autoloop-triage.md`：`scripts/autoloop_run.py` 接入 agent-autoloop 后的只读排障手册。
