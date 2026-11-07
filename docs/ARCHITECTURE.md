# 拾穗架构设计

包名 `litgleaner`。本文档只描述要实现的功能和设计，是实现的依据；进度和实施顺序见 [ROADMAP.md](ROADMAP.md)，实测记录见 `docs/experiments/`。要改变这里的决策，先改本文档或新增 `docs/adr/` 记录，再改代码。

## 1. 目标与边界

**目标**：研究者写下研究背景、研究内容和研究目标，系统每天从多个学术来源检索文献，按研究画像筛选、评分、提炼，通过飞书卡片推送给研究者本人或 agent，并在自带的领读器里帮助研究者读懂、记住、用上这些论文。本机 Zotero 作为可选同步。

**目标用户**：学生和研究者，首要是研一和研零。用户在自己的电脑上双击安装运行，进阶用户也可以部署到服务器。项目自包含：推送、按钮回调、阅读、写入 Zotero 等用户功能都由拾穗自己实现，不依赖使用者本机的其他自动化平台。需求和证据见 [REQUIREMENTS.md](REQUIREMENTS.md)。

**解决的问题**：

| 研究者的痛点 | 系统的回应 |
|---|---|
| 文献太多、怕漏，检索式难写，没人教 | 用中文研究描述起草英文检索式并解释每个词，入门清单，多来源跟踪 |
| AI 摘要会夸大结论，编造文献 | 评分、理由、回答都带原文引用和页码，代码校验引用存在；元数据从不经过模型 |
| 读完留不下，说不出和课题的关系 | 组会视角的读后卡片，推荐理由逐条对照研究目标 |
| 读得慢，术语不懂，越追引用越散 | 领读器：读前背景卡、术语就地释义、按需段落翻译、边读边问、前置阅读指引 |
| 笔记散、找不回 | 批注和卡片集中存放、可检索、能跳回原文；跨论文对比表 |
| 存了不读，跟不上 | 待读队列、积压时减少推送、主题周报 |
| 写综述怕 AI 率和假引用 | 综述骨架和对比表，不代写正文；引用导出只用数据源给出的标识符 |
| 工具配不起来、不想付费 | 桌面安装包和首次向导，支持低价模型并显示花费，开源免费 |

**不做**：绕过授权下载（Sci-Hub、代下载）、代写正文、内置受授权限制的期刊分区数据、引文图谱可视化、出版商网站爬取、公开的网页服务。

**必须容易变的东西**：

| 变化点 | 改动方式 | 不应该发生的事 |
|---|---|---|
| 增减信号源 | 加一个适配器和一段配置，或把 `enabled` 改成 `false` | 改流程代码 |
| 改评价指标 | 改一个 JSON 文件 | 改代码；新旧分数混在一起比较 |
| 切换或修改研究画像 | 改一个主题文件并执行 `activate` | 重新下载、重新解析已有文献 |
| 增减推送渠道 | 加一个发布适配器和路由配置 | 评分或选文代码感知渠道 |
| 换解析器（云端↔本地） | 换适配器 | 切分和评估代码感知 MinerU 格式 |
| 换翻译模型 | 改 `llm.yaml` 中 `translate` 任务的模型 | 领读器代码感知模型厂商 |
| 换双语 PDF 工具 | 换 `Translator` 适配器 | 导出代码感知翻译工具 |
| 换文献管理软件 | 加 `ReferenceLibrary` 适配器 | 流程代码出现 Zotero 字段 |
| 换存储位置（本地↔对象存储） | 改配置 | 业务代码出现文件路径 |

## 2. 三条核心原则

1. **事实与判断分离。** 论文的元数据、全文、提取内容、译文是事实，全局只存一份。相关性、分数、是否推送是判断，按“主题 + 评价标准”分别存放。这是切换主题和修改指标不用重来的根本原因。
2. **一切派生物都带来源指纹。** 每个派生结果都记录“输入哈希 + 生产者版本”。配置变了，系统能算出哪些结果失效（`litgleaner stale`），而不是全量重跑，也不静默重跑。
3. **变化点走接口，其余保持简单。** 只给已知会有多种实现的地方定义接口，其他地方不做抽象（见第 16 节）。

## 3. 总体流程

```text
 配置: topics(研究画像) / rubrics / sources / channels / llm / retention
   │
   └─ topic draft：画像 + 种子论文（含 Zotero 分组）→ 起草检索式 → 用户确认
                          │
 ┌──────────── 全局：事实层（每篇只做一次）──────────────┐
 │ discover → resolve → acquire → parse → extract        │
 │  发现       去重合并   取全文   归一化    提取事实与对比字段 │
 └──────────────────────────────────────────────────┘
        │ 新论文（先过本地关键词过滤）   ▲ 初筛通过才取全文
        ▼                              │
 ┌──────────── 按主题：判断层 ──────────────────────────┐
 │ triage ───────────────┘   assess → score → select    │
 │ 初筛相关性                 分维度评估+对照目标 算分 选文   │
 └──────────────────────────────────────────────────┘
                          │
 ┌──────────── 输出层 ──────────────────────────────────┐
 │ Digest → 可见性投影 → 渲染 → 发布 → 飞书等渠道（人和 agent） │
 │ 周报：待读提醒 + 主题进展                               │
 │ 导出：方法对比表、引用（BibTeX / CSL-JSON）              │
 └──────────────────────────────────────────────────┘
                          │
 ┌──────────── 领读与沉淀 ───────────────────────────────┐
 │ 卡片“去读” → 领读器：背景卡、释义、按需翻译、提问、读后卡片 │
 │ 批注、卡片编辑 → annotation；阅读进度 → reading_state     │
 │ 可选：“加入 Zotero” → library：条目、笔记、PDF           │
 │ 可选：litgleaner mcp 给外部 agent 查画像、证据、相关论文    │
 └──────────────────────────────────────────────────┘
 审计：覆盖率、漏检、误杀抽查、渠道健康（横切全流程）
```

初筛通过后才取全文和解析，这是控制成本和存储的主要手段。

## 4. 模块与依赖规则

```text
litgleaner/
├─ config/
│  ├─ app.yaml  sources.yaml  channels.yaml  llm.yaml  retention.yaml
│  ├─ journals/<set>.yaml        # 期刊清单，可被多个主题引用
│  ├─ topics/<topic-id>.yaml     # 一个研究画像一个文件
│  └─ rubrics/<rubric-id>.json   # 评价指标，唯一来源
├─ prompts/                      # 提示词是文件，不写在代码里
├─ src/litgleaner/
│  ├─ domain/       # 模型与纯逻辑：Paper、Rubric 算分、切分器、去重、关键词过滤
│  ├─ ports/        # 接口，见下表
│  ├─ application/  # 各阶段、任务调度、选文、周报、导出、给 MCP 用的查询
│  ├─ adapters/     # 外部实现，每个外部系统一个包
│  │  ├─ common/    # 适配器共享：HTTP 客户端、PDF 下载、游标窗口、选项解析
│  │  ├─ sources/ fulltext/ parsers/ llm/ renderers/ blobstore/
│  │  ├─ feishu/    # 卡片推送 + 按钮回调
│  │  ├─ zotero/    # 收件箱信号源 + 全文来源 + 文献库写入与读取
│  │  ├─ translators/  # BabelDOC 等
│  │  └─ fakes/     # 测试和演示用的假实现
│  ├─ infra/        # 数据库、迁移、配置加载、钥匙串、应用数据目录
│  ├─ bootstrap.py  # 组装程序，含适配器注册表（显式字典）
│  ├─ service.py    # 常驻服务：组合网页后端、调度器、渠道监听、任务执行
│  ├─ server/       # 领读器的本地 HTTP 接口（只做协议适配）
│  ├─ cli.py  cli_library.py  cli_shared.py
│  └─ mcp_server.py # 只读 MCP 入口
├─ web/              # 领读器前端（TypeScript），构建产物随安装包发布
├─ packaging/        # 各操作系统的打包脚本和安装包配置
├─ tests/  unit/ contracts/ integration/ e2e/ architecture/ fixtures/
├─ data/             # 开发时的数据库、文件、归档（不进 git）；安装版放在系统应用数据目录
└─ docs/  REQUIREMENTS.md  ARCHITECTURE.md  ROADMAP.md  adr/  experiments/
```

| 接口（`ports/`） | 职责 | 典型实现 |
|---|---|---|
| `Source` | 按查询返回论文记录 | Crossref、OpenAlex、arXiv、PubMed、Zotero 收件箱 |
| `FullTextProvider` | 按标识符找 PDF | Zotero、arXiv、Unpaywall、Crossref TDM |
| `Parser` | PDF → `NormalizedDocument` | MinerU 云端、`pdf_text` |
| `LLM` | 结构化输出调用 | Anthropic Messages、OpenAI Chat / Responses、Gemini |
| `Renderer` / `Publisher` | 渲染与发送 | 飞书卡片 |
| `CardClick`（`ports/inbound.py`） | 渠道按钮回调的统一格式 | 飞书 |
| `ReferenceLibrary` | 写入条目、附件，读取分组和标签 | Zotero 本地 API |
| `Translator` | PDF → 双语 PDF（可选导出） | BabelDOC |
| `ImageGenerator` | 分镜 → 动图（可选） | GPT Image |
| `SecretStore` | 读写密钥 | 系统钥匙串、环境变量 |
| `BlobStore` | 按 `blob://` 地址存取文件 | 本地目录、对象存储 |
| `Store` | 数据库访问（只用来维持依赖方向） | SQLite |

**依赖方向只能向内**，由 `import-linter` 强制：

```text
入口：service（组合 server、调度、监听）；cli → cli_library → cli_shared；mcp_server
  → bootstrap → adapters | infra → application → ports → domain
```

- 入口层之间不互相依赖业务逻辑。`server/` 和 `mcp_server.py` 只做协议适配，查询和操作都调用 `application` 中的函数，和命令行共用同一套实现。
- `web/` 是独立的前端工程，只通过 `server/` 的 HTTP 接口取数据，不读数据库和文件。

- `domain` 不做任何 IO，不知道 MinerU、飞书、Zotero、SQLite 的存在。
- 适配器之间不能互相导入，也不能访问 `application` 和 `infra`；共享代码只能放 `adapters/common/`。
- 只有 `adapters/`、`bootstrap.py` 和配置里可以出现具体厂商名。`application`、`ports` 中出现厂商名（包括大模型厂商、飞书、BabelDOC）视为缺陷。

## 5. 数据模型

存储用 SQLite（WAL 模式）+ SQLAlchemy Core + Alembic，表结构只通过迁移改变。

| 层 | 表 | 主键或唯一键 | 说明 |
|---|---|---|---|
| 事实 | `paper` | 内部 ULID | 标题、摘要、作者、期刊、日期 |
| 事实 | `paper_alias` | `(scheme, value)` | DOI、arXiv ID 等都映射到同一篇；只由信号源写入 |
| 事实 | `merge_log` | 自增 | 合并记录，支持撤销 |
| 事实 | `merge_candidate` | `(paper_a, paper_b)` | 模糊匹配到、等待人工确认的疑似重复 |
| 事实 | `sighting` | `(paper, source, query_hash)` | 谁在何时发现了它，覆盖率统计的基础 |
| 事实 | `source_cursor` | `(source, query_hash)` | 增量抓取水位 |
| 事实 | `artifact` | `(sha256, kind)` | PDF、归一化文档、解析器原始输出、双语 PDF 等文件的索引、许可依据和保留期 |
| 事实 | `fulltext` | `paper` | 全文状态：`available`、`parsed`、`unavailable`、`failed` |
| 事实 | `extraction` | `(paper, input_hash, extractor_version)` | 结论、不足、创新点、方法、数据、指标、结果，均带证据 |
| 事实 | `translation` | `(unit, source_hash, target_lang, glossary_hash, translator_version)` | `unit` 为 `block`（段落译文）或 `pdf`（双语 PDF 的 `artifact`） |
| 事实 | `explanation` | `(paper, kind, input_hash, producer_version)` | 领读器生成的背景卡、章节要点、前置阅读指引，带证据 |
| 判断 | `topic_state` | `topic` | 是否启用、当前画像哈希、从何时开始初筛 |
| 判断 | `triage` | `(paper, topic, topic_hash)` | 初筛结果和理由，提示词版本和模型只作记录 |
| 判断 | `assessment` | `(paper, topic, topic_hash, semantic_hash, input_hash)` | 各维度等级、证据、目标对照，由 LLM 产生 |
| 判断 | `score` | `(assessment, policy_hash)` | 最终分、等级、是否临时、是否有无法判断的维度，由代码算出 |
| 判断 | `rubric_snapshot` | `hash` | 首次使用时保存指标全文，历史分数可追溯 |
| 输出 | `digest` / `digest_item` | ULID / `(digest, position)` | 一次推送的不可变内容 |
| 输出 | `delivery` | `(digest, channel)` | 发送状态，保证不重复推送 |
| 反馈 | `feedback` | ULID；渠道事件号 `external_id` 唯一 | 只追加：加入文献库、不感兴趣、有用、需要全文 |
| 反馈 | `library_entry` | `(paper, library)` | 已写进文献库的条目，有这一行就不再写 |
| 反馈 | `reading_state` | `paper` | 待读、在读（含读到的位置）、已读、放弃；来自领读器、渠道反馈和 Zotero 标签 |
| 用户 | `annotation` | ULID | 高亮、批注、读后卡片的用户修改；记录页码、`bbox` 和原文片段，不依赖 `chunk_id` |
| 用户 | `qa_message` | ULID | 边读边问的问答记录，回答附引用和核验结果 |
| 反馈 | `inbox` | ULID | 手动投喂的论文和邮件提醒，待处理 |
| 审计 | `miss_log` | ULID | 漏检记录及原因 |
| 运行 | `job` | 幂等键 | 任务队列、重试、推迟、死信 |
| 运行 | `llm_call` | 自增 | 任务、模型、token、费用，用于预算控制 |

**去重规则**：

- 标识符完全相同才自动合并，新标识符挂到已有论文上。两篇已有论文互相冲突时只记录、不自动合并。
- 标题加作者的模糊匹配：高置信时合并，否则写入 `merge_candidate`，由 `litgleaner merge list / accept / reject` 处理。所有合并都写 `merge_log`，可以撤销。
- **预印本和正式版是同一篇论文。** 信号源同时给出两个标识符（如 OpenAlex、Semantic Scholar 的外部编号，Crossref 的 `is-preprint-of` 关系）时自动关联，元数据以正式版为准。正式版出现不会自动重取全文、重新评估；`stale` 报告会列出这些论文，由用户显式 `reassess`。

## 6. 信号源与全文

### 6.1 接口

适配器声明自己支持哪些查询，由流程规划实际调用，适配器不认识“主题”这个概念：

```python
Query = JournalWatch | KeywordSearch | CitationWatch | AuthorWatch | InboxDrain

class Source(Protocol):
    name: str
    capabilities: frozenset[type]   # 支持的查询类型

    def fetch(self, query: Query, cursor: Cursor | None) -> FetchResult: ...
    # FetchResult = records、next_cursor、warnings
    # 错误类型统一为 RateLimited / AuthFailed / SchemaChanged / Transient
```

| 能力 | 典型适配器 |
|---|---|
| `JournalWatch` | Crossref |
| `KeywordSearch` | arXiv、OpenAlex、PubMed、Europe PMC、CORE |
| `CitationWatch`、`AuthorWatch` | Semantic Scholar、OpenAlex |
| `InboxDrain` | Zotero 新加入的条目、手动投喂、邮件提醒 |

### 6.2 配置

```yaml
sources:
  crossref:
    adapter: crossref
    enabled: true
    options: { rate_limit_per_sec: 1 }
  core:
    adapter: core
    enabled: false
    credentials_env: [CORE_API_KEY]
  semantic_scholar:
    adapter: semantic_scholar
    enabled: true
    optional_env: [SEMANTIC_SCHOLAR_API_KEY]
```

### 6.3 规则

- **加来源**：写一个适配器，在 `bootstrap.py` 的注册表里加一行，加配置，录制响应到 `tests/fixtures/<来源>/`，通过 `tests/contracts/` 的统一测试。
- **减来源**：`enabled: false`。历史 `sighting` 保留，审计时仍能看到它的贡献。
- **原始响应先落盘，再解析**。解析逻辑修好后用 `litgleaner replay <来源> --since` 从落盘数据重放，不必重新抓取。
- 限速、错误分类、密钥脱敏和原始留档由共享 HTTP 客户端（`adapters/common/http.py`）负责，适配器不各写一套；重试和退避由任务调度负责。
- 游标统一是 ISO 日期，每次与上次窗口重叠几天以接住晚入库的记录（`adapters/common/window.py`）。Zotero 例外：本地模式用加入时间，Web 模式用库版本号。
- 联系邮箱从 `LITGLEANER_CONTACT_EMAIL` 读取，所有来源共用；可选 key 在配置里用 `optional_env` 声明，缺失时 `doctor` 只警告。
- 同一个 API 的元数据发现和全文获取是两个独立适配器，分别实现 `Source` 和 `FullTextProvider`。
- 各来源的能力和已知限制见 [SOURCES.md](SOURCES.md)。

### 6.4 全文获取

`FullTextProvider` 按配置顺序依次尝试：用户自己的 Zotero → 预印本（arXiv、PMC 开放子集）→ 开放获取聚合（Unpaywall、OpenAlex、Semantic Scholar、CORE）→ 出版商在 Crossref 登记的文本挖掘链接（订阅文章需机构网络）→ 手动投喂 → 仅摘要。

- 单个来源出错时继续尝试下一个；全部没拿到且有错误时交给调度重试，不标记为“没有全文”。
- 第三方 PDF 链接统一由 `adapters/common/download.py` 下载：每一跳都检查不是内网地址，限制大小和总耗时，校验 PDF 文件头。
- 每个 `artifact` 记录 `license_basis`（`open` / `subscribed` / `user_supplied`），决定保留策略，并保证这些文件不会进入公开渠道。

## 7. 研究画像

### 7.1 文件

一个研究画像一个文件，允许多个画像同时运行。文件只存定义，是否启用记录在数据库（`topic_state`），由命令切换。

```yaml
id: example-topic
name: 示例课题
background: >
  研究背景：领域现状，要解决的问题为什么重要。
content: >
  研究内容：打算研究什么、用什么方法、关心什么、不关心什么。
goals:
  - id: g1
    text: 研究目标一
  - id: g2
    text: 研究目标二
query:                          # 由 topic draft 起草，用户确认后写入
  include: ["keyword a", "keyword b"]
  synonyms:
    keyword a: ["别名 1", "别名 2"]
  exclude: ["不相关的词"]
journal_sets: [core]
seeds:
  dois: ["10.1109/EXAMPLE.2026.0000001"]
  authors: []
  zotero_collection: "课题/核心文献"   # 本机 Zotero 分组，作为种子论文
rubric: default
public: false                   # 是否允许出现在公开渠道
```

- `topic_hash` 覆盖影响判断的字段：`background`、`content`、`goals`、`query`。改名称或公开设置不会让判断失效。
- 种子论文同时用于起草检索式、引文追踪和校准集。
- 背景、内容、目标可以用中文写；检索式和术语表由系统生成英文与中文对照。
- 安装版的研究画像存放在应用数据目录，通过领读器的设置页编辑；文件格式不变，命令行和网页编辑的是同一份文件。

**课题术语表**：每个研究画像配一个 `<topic-id>.glossary.yaml`，每条包含英文术语、中文译名、一句话释义和出处。

- 由 `topic draft` 根据画像和种子论文起草，用户在领读器里可以随时增删改。
- 翻译和就地释义必须使用术语表中的译名；术语表内容的哈希（`glossary_hash`）计入译文指纹。
- 术语表变化后旧译文不自动重译，打开论文时提示“术语表已更新”，由用户选择是否重译。

**期刊分级**：`journals/<set>.yaml` 的每本期刊可以带 `tier` 标签（如“中科院一区”“CCF-A”）。

- 分级数据由用户导入：导师或学校给的 CSV，在设置页导入。项目不内置受授权限制的分区数据。
- 卡片显示期刊分级，路由可以按分级过滤。

### 7.2 起草检索式

研究者通常不擅长写检索式，所以由系统起草、研究者确认：

```bash
litgleaner topic draft example-topic    # 显式调用 LLM，花少量费用
```

- 输入：背景、内容、目标，以及种子论文的标题和摘要（DOI 列表 + Zotero 分组，后者通过 `ReferenceLibrary` 读取）。
- 输出写到 `<id>.draft.yaml`，同时显示和当前文件的差异：
  - 建议的 `include`、`synonyms`、`exclude`，以及术语表草稿；
  - 每个检索词都附**选择理由**（对应哪段描述、为什么用这个学科表述），兼作检索方法的教学；
  - 每个检索词预估能搜到多少篇（向 OpenAlex 查询计数，不花模型费用），帮助判断太宽或太窄；
  - 根据种子论文的期刊统计（代码统计，不经 LLM）给出期刊集建议。
- 草稿不会自动生效。研究者在领读器设置页或文件里确认合并后，`topic activate` 才使用新定义。

**入门清单**：新研究画像第一次启用时，按检索式从 OpenAlex 取综述、高被引经典论文和近两年的代表作，生成一份一次性推荐，标明“先读综述”的顺序。

### 7.3 切换与回填

- 画像变化后，**只有新发现的论文按新定义处理**，运行时提示执行 `backfill`。旧定义下的判断不再参与排序，但不会自动花钱重筛历史。
- 新启用的画像同样只处理之后发现的论文，历史论文必须显式回填：

```bash
litgleaner topic activate example-topic
litgleaner topic backfill example-topic --since 2024-01-01             # 预览数量和费用
litgleaner topic backfill example-topic --since 2024-01-01 --execute   # 确认执行
```

- `backfill` 先用库内已有元数据重新初筛（只花初筛费用），再向支持历史查询的信号源补查。已有的 PDF、解析结果和提取内容直接复用。暂停的画像不消耗资源，数据全部保留。

### 7.4 初筛前的本地过滤

`domain` 中的纯函数先按 `include`、`synonyms`、`exclude` 在标题和摘要上做关键词过滤，命中排除词或完全不相关的论文不进入 LLM 初筛，结果和原因照样写入 `triage`（模型记为 `rule`）。期刊监控类来源无法按关键词查询，这一步是控制初筛费用的主要手段。

## 8. 评价指标

### 8.1 唯一指标

全系统只有一个排序和选文依据：`reading_value`（阅读价值分，0～100）。各维度只是它的组成部分，不单独用于选文。`config/rubrics/<id>.json` 是唯一来源，代码和提示词里都不写死维度或权重。

```json
{
  "schema_version": 1,
  "id": "default",
  "metric": { "key": "reading_value", "label": "阅读价值分", "range": [0, 100] },
  "levels": { "min": 0, "max": 5 },
  "dimensions": [
    {
      "key": "relevance",
      "label": "相关性",
      "weight": 35,
      "requires_fulltext": false,
      "question": "这篇论文和当前研究画像的目标有多接近？",
      "anchors": { "0": "完全无关", "3": "同领域，可借鉴", "5": "直接解决某个研究目标" }
    },
    {
      "key": "novelty",
      "label": "创新性",
      "weight": 25,
      "requires_fulltext": false,
      "question": "是否提出了新方法或新发现？",
      "anchors": { "0": "重复已有工作", "3": "对已有方法有实质改进", "5": "提出全新思路并显著突破" }
    },
    {
      "key": "evidence",
      "label": "证据可信度",
      "weight": 20,
      "requires_fulltext": true,
      "question": "实验设计、基线对比和数据是否支撑其结论？",
      "anchors": { "0": "没有实证", "3": "有基线对比但存在明显局限", "5": "设计严谨且结果可复现" }
    },
    {
      "key": "reusability",
      "label": "可复用性",
      "weight": 20,
      "requires_fulltext": true,
      "question": "方法、代码或数据能否用到我的研究中？",
      "anchors": { "0": "无法复用", "3": "方法可借鉴但需较多适配", "5": "代码和数据公开，可以直接使用" }
    }
  ],
  "gates": [{ "dimension": "relevance", "min_level": 2 }],
  "tiers": [
    { "key": "must_read", "label": "必读", "min_score": 80 },
    { "key": "digest", "label": "周报", "min_score": 60 },
    { "key": "drop", "label": "不推送", "min_score": 0 }
  ],
  "provisional": { "tier": "need_fulltext", "min_score": 60 }
}
```

### 8.2 计算规则

由 `domain/rubric.py` 的纯函数完成，**LLM 只给等级和证据，不给总分**：

```text
reading_value = round( Σ(weight × level / levels.max) / Σ(weight) × 100 )   # 只累加有等级的维度
```

维度等级为空有两种原因，处理方式不同：

| 情况 | 判定 | 处理 |
|---|---|---|
| **缺全文**：只有摘要，`requires_fulltext` 的维度 | 代码按全文状态置空，不看模型回答 | 结果标记为临时分 `provisional`，不能进入 `must_read`；达到 `provisional.min_score` 时归入 `need_fulltext`，推送时附“需要全文”操作 |
| **无法判断**：有输入但模型给不出等级 | 模型返回空 | 只按可判断维度计算，不限制等级，结果标记 `undetermined` 并在卡片上注明哪些维度无法判断 |

- **门槛**：门槛维度低于 `min_level` 时直接归入兜底等级（`drop`）。门槛维度为空时同样不放行，并进入误杀抽查。
- 启动时校验：权重总和为 100、等级和门槛合法、`tiers` 完整、`provisional.tier` 不与普通等级重名。校验失败直接退出。

### 8.3 修改指标的两种情况

直接改 JSON，git 记录历史，系统保存快照。系统用两个哈希判断影响范围：

| 修改内容 | 变化的哈希 | 处理方式 | 费用 |
|---|---|---|---|
| 权重、门槛、等级线 | `policy_hash` | `litgleaner rubric rescore`，用已有维度等级重算 | 零 LLM 费用 |
| 增删维度、问题、锚点 | `semantic_hash` | `litgleaner rubric reassess <主题>` 先预览，加 `--execute` 按范围重评 | 有 LLM 费用 |

只有 `semantic_hash` 和 `policy_hash` 都相同的分数才能直接比较。选文只使用当前画像和当前指标下的分数，没有重算的旧分数不参与排序。

**任何配置修改都不会静默产生费用**：改维度定义、研究画像、提示词或模型后，系统不会自动重评已有论文，新论文才用新配置。`litgleaner stale` 列出哪些派生结果的指纹与当前配置不一致（零费用），由用户决定是否重做。已归档的论文不会因此被删除，归档文件保留旧结果并标记 `stale: true`。

`litgleaner rubric eval` 用校准集（种子论文）和反馈数据比较修改前后的效果（前 K 篇命中率、必读漏选率）。改指标先看这个结果，不凭感觉。

### 8.4 对照研究目标

评估时模型同时输出 `goal_links`：这篇论文对应哪个研究目标（`goals[].id`）、可以借鉴什么、依据的原文引用和页码。

- `goal_links` 只用于推荐理由和周报，**不参与算分**；分数仍只由维度等级决定。
- 引用不在原文中的条目标记 `unverified`，推送时不显示为“作者原话”。
- 引用了不存在的目标 id 的条目丢弃。

## 9. 文献处理：解析与切分

### 9.1 防腐层：统一文档格式

不同解析器、不同版本的输出结构不同，所以每个解析适配器都必须转换成 `NormalizedDocument`。下游只认这个格式：

```python
class Block:            # 不可变
    kind: Literal["heading", "text", "table", "equation", "figure", "code", "reference"]
    text: str
    page: int
    bbox: tuple[int, int, int, int] | None   # 用于从 PDF 恢复高清图和定位证据
    caption: str | None

class NormalizedDocument:
    schema_version: int
    source_pdf_sha256: str
    parser_id: str            # 如 mineru_cloud@vlm
    blocks: tuple[Block, ...]
    front_matter: tuple[str, ...]   # 页脚中的作者机构、基金信息，不进入正文块
    quality: Literal["ok", "degraded"]
```

默认使用 MinerU 精准 API 的 `vlm` 模型（适配器 `mineru_cloud`）。不使用只返回 Markdown 的轻量接口，因为它没有页码、坐标和结构信息，生成不了上面的格式。不耗额度的备选是 `pdf_text`，英文论文效果接近，中文期刊明显较差（对比见 [parser-comparison](experiments/2026-11-05-parser-comparison.md)）。

**MinerU 字段映射**（`*_content_list.json`）：

| MinerU `type` | 关键字段 | 归一化处理 |
|---|---|---|
| `text` | `text`、`text_level`、`bbox`、`page_idx` | `text_level` 非空则为 heading，否则为 text |
| `equation` | `text`（LaTeX）、`text_format` | equation |
| `table` | `table_body`（HTML）、`table_caption`、`table_footnote` | table |
| `chart`、`image` | `img_path`、`*_caption`、`*_footnote`、`content` | figure |
| `code` | `code_body`、`code_caption`、`sub_type` | code |
| `ref_text` | `text` | reference |
| `page_footnote` | `text` | 写入 `front_matter` |
| `page_number`、`aside_text`、`header`、`footer` | `text` | 丢弃 |

未知类型不能静默丢弃：保留文字并把文档标记为 `degraded`。

### 9.2 切分规则

切分器是 `domain/chunker.py` 的纯函数，相同输入和版本得到完全相同的结果。

```text
NormalizedDocument
  → 按标题划分章节，并归入标准类型：
    abstract / introduction / method / results / discussion /
    conclusion / limitation / references / acknowledgment / other
  → 每个章节内按“段落 → 句子”逐级切分
```

| 规则 | 取值 | 原因 |
|---|---|---|
| 目标大小 | 约 800 token，上限 1200 | 足够容纳一个完整论点 |
| 切分边界 | 优先章节和段落，迫不得已才按句子 | 保持语义完整 |
| 重叠 | 默认不重叠；长段落被迫切开时重叠 1 句 | 减少冗余存储和 token |
| 表格、图、代码 | 独立成块，不拆开 | 拆开后失去意义 |
| 公式 | 不拆开，但留在上下文的块中 | 公式离开前后文就看不懂 |
| 参考文献 | 单独解析成结构化列表，不进入正文块 | 用于引文追踪，不浪费 token |
| 致谢、页眉页脚 | 丢弃 | 对评估无用 |

每个 chunk 的字段：`chunk_id`、`section_type`、`section_path`、`page_start`、`page_end`、`token_count`、`text`。参数放在配置里，修改后 `chunker_version` 随之变化，并计入提取和评估的输入指纹。

**标题识别不到时不能静默出错**：先用规则匹配，缺少结论章节时取参考文献前的最后两节并标记 `degraded`。低质量解析结果会反映在推送内容里。

切分器必须处理以下情况，并有对应测试：

- 标题可能带空格：小型大写拆成 `I NTRODUCTION`，中文标题写成 `5 总 结`、`参　考　文　献`，分类时也检查去掉空格的写法。
- 中文期刊的双语前置部分：页首英文标题不算正文开始；`Abstract In ...`（没有破折号）、`Key words`、`摘　要`、`结束语` 都要识别。
- 参考文献后可能有作者简介和照片：解析器标出了参考文献条目时只用这些条目。
- `text_level` 不能表示层级：章节嵌套靠编号（`I.`、`A.`、`1.1`）推断。
- 摘要不是标题：IEEE 的 `Abstract—...`、`Index Terms—...` 是普通段落，要按段首模式识别。
- 论文标题以元数据为准，解析结果里的 1 级标题只作校验。
- 图注关联不可靠：评估不能依赖“图和图注一定对应”。
- 解析文本不等于原文：引用校验只能证明“引文存在于解析文本”，推送中的公式和关键数字提示以原文为准。

### 9.3 切分的用途

1. **节省 LLM 费用**：提取和评估只读摘要、引言、方法概述、结果、讨论、结论，并设总 token 上限。
2. **证据可追溯**：提取和评估结果记录原文引用和页码，代码校验引用确实存在于原文；校验失败标记为 `unverified`，不当作事实。证据保存引用原文和页码，不只存 `chunk_id`，切分规则升级后旧证据依然有效。
3. **文库检索**：归档论文的 chunk 写入 SQLite FTS5，供 MCP 和周报检索。

### 9.4 提取内容

`extraction` 与主题无关，每篇只做一次，切换主题时直接复用：

| 字段 | 内容 | 证据 |
|---|---|---|
| `summary` | 一句话概括（模型概括） | — |
| `problem` | 要解决的问题 | 原文引用 + 页码 |
| `prior_gap` | 已有方法的不足（作者的说法） | 原文引用 + 页码 |
| `approach` | 本文做法 | 原文引用 + 页码 |
| `claims` | 作者的主要结论 | 原文引用 + 页码，必填 |
| `innovation` | 创新点（模型概括） | 可选引用 |
| `limitations` | 不足，区分作者自述和模型推断 | 作者自述必须有引用 |
| `method` | 方法类别和要点 | 引用 + 页码 |
| `datasets` | 使用的数据集 | 引用 + 页码 |
| `metrics` / `results` | 指标、取值、实验设置 | 引用 + 页码 |
| `code_data` | 代码和数据是否公开、链接 | 链接来自原文时才保留 |
| `questions` | 组会上可能被问到的 3 个问题及回答要点 | 回答要点引用原文 |
| `highlights` | 原文中代表目标、创新、方法、结果的句子 | 原文片段 + 页码 + `bbox`，供领读器分色高亮 |

提取字段变化会改变 `extractor_version`，旧提取结果不会自动重做，由 `stale` 报告列出。

**读后卡片**是 `extraction`（与主题无关）、`assessment.goal_links`（和我课题的关系）以及用户在 `annotation` 中的修改合成的视图，不单独存一份。用户修改不覆盖模型结果，两者都保留，并标明哪部分是用户写的。

## 10. 存储

### 10.1 放在哪里

| 内容 | 位置 | 能否重建 |
|---|---|---|
| 配置、提示词、代码 | 仓库 | — |
| 数据库 | `<数据目录>/db/litgleaner.db` | 否，**最重要的数据**，包括批注、问答、反馈、阅读状态等无法重建的记录 |
| JSON 归档 | `<数据目录>/archive/papers/YYYY/MM/<paper_id>.json` | 能，由数据库导出 |

`<数据目录>`：开发时是仓库下的 `data/`；安装版是系统的应用数据目录（macOS 为 `~/Library/Application Support/litgleaner`，Windows 为 `%APPDATA%\litgleaner`，Linux 为 `$XDG_DATA_HOME/litgleaner`），可在设置中更改。下文 `blob://` 地址都相对于它。
| 原始 API 响应 | `blob://raw/<source>/YYYY/MM/DD.jsonl.zst` | 大多能，但会过期 |
| PDF | `blob://pdf/ab/cd/<sha256>.pdf` | 开放文献能，付费文献不一定 |
| 双语 PDF（可选导出） | `blob://translated/ab/cd/<sha256>.<lang>.pdf` | 能，但消耗翻译费用 |
| 段落译文、背景卡等讲解 | 数据库 `translation`、`explanation` | 能，但消耗模型费用 |
| 归一化文档 | `blob://doc/ab/cd/<sha256>.<parser>.json.zst` | 能，但消耗解析额度 |
| 解析器原始输出 | `blob://parser_raw/<sha256>.content_list.json.zst`（只留这一个文件） | 能 |
| chunk | 不单独存，需要时计算；归档论文写入 FTS | 能，纯函数 |

**真相只有一处**：SQLite 是运行时的唯一真相。JSON 归档由 `archive` 阶段导出，不手改、不反向写入、不进 git。业务代码只使用 `blob://` 地址和 `BlobStore` 接口，因此可以把文件从本地迁到对象存储而不改业务代码。

### 10.2 如何减少存储

1. **漏斗**：初筛没通过的论文不下载 PDF，只存元数据和摘要。这是效果最大的一项。
2. **内容寻址**：文件以 SHA-256 命名，相同 PDF 只存一份。
3. **压缩**：文本和 JSON 用 zstd 压缩。
4. **解析后瘦身**：解析器结果包解压后立即丢弃 `layout.json`、`model.json`、`full.html`、`full.tex`、`full.docx`、`origin.pdf` 和缩略图，`content_list.json` 只留 30 天；需要配图时按 `bbox` 从 PDF 恢复高清原图。
5. **按价值分级保留**（`retention.yaml`）：

| 论文状态 | 元数据和摘要 | 提取结果 | 归一化文档 | PDF 与双语 PDF |
|---|---|---|---|---|
| 初筛淘汰 | 永久 | 无 | 无 | 从未下载 |
| 深评 `drop` | 永久 | 永久 | 90 天 | 30 天 |
| 深评 `digest` | 永久 | 永久 | 永久 | 180 天 |
| `must_read` 或已加入文献库 | 永久 | 永久 | 永久 | 永久 |
| 用户上传或付费获取且未归档 | 永久 | 永久 | 90 天 | 90 天 |

元数据和摘要永久保留，切换主题时才有东西可以重新初筛；提取结果体积很小，也永久保留。清理由 `litgleaner gc --dry-run` 预览后执行，只有相关 `job` 全部完成后才能删除对应文件。`litgleaner stats` 报告条数和文件占用。

### 10.3 备份

`litgleaner backup` 用 `VACUUM INTO` 生成数据库快照，按配置保留若干份，可上传对象存储；`litgleaner restore --check <快照>` 在临时目录还原并校验迁移版本和行数，用于定期恢复演练。

- 安装版默认每天自动备份到数据目录下的 `backups/`，保留 14 份。
- 每次升级执行数据库迁移前，必须先自动备份一次。

## 11. 推送

### 11.1 分层

```text
Selector（按路由选文）
  → Digest（不可变内容，存库）
  → 可见性投影（private / public）
  → Renderer（飞书卡片 / 纯文本 / …）
  → Publisher（只负责发送）
  → delivery（记录状态，失败重试，不重复发送）
```

选文和内容与渠道完全无关，一份 Digest 可以发到多个渠道。研究者本人和 agent 通过同一套渠道接收：卡片带论文编号和 DOI，agent 可以凭编号通过 MCP（第 12.6 节）取完整内容。

```python
class Publisher(Protocol):
    name: str
    visibility: Literal["private", "public"]

    def publish(self, rendered: Rendered) -> Receipt: ...
```

### 11.2 配置：渠道和路由分开

```yaml
channels:
  feishu_me:
    adapter: feishu
    renderer: feishu_card
    visibility: private
    credentials_env: [FEISHU_APP_ID, FEISHU_APP_SECRET]
    options: { receive_id: ou_xxx, receive_id_type: open_id }

routes:
  - { id: daily-me, topic: "*", channel: feishu_me, schedule: "daily 08:30", tiers: [must_read, need_fulltext], limit: 5 }
  - { id: weekly-me, topic: "*", channel: feishu_me, schedule: "weekly mon 09:00", tiers: [digest], limit: 15 }
  - { id: review-me, kind: review, topic: "*", channel: feishu_me, schedule: "weekly fri 17:00" }
```

`kind: review` 的路由生成周报（第 13 节），其余路由生成推荐。

### 11.3 推荐卡片的内容

每篇论文显示：

- 标题、作者、期刊和期刊分级、日期、DOI 链接；阅读价值分和等级；命中的所有主题。
- **为什么推给你**：按 `goal_links` 逐条列出“对应目标 g1：可以借鉴什么”。
- **作者原话**：一到两条核实过的引用，带页码；与模型概括在视觉上分开，模型概括标明“模型概括”。
- **全文获取途径**：开放获取、需要校园网（出版商链接）或暂无全文。
- 临时分注明“只读了摘要”；`undetermined` 注明哪些维度无法判断。
- 私有渠道带操作：「去读」（打开领读器中的这篇论文）、「稍后读」「不感兴趣」；开启 Zotero 同步时另有「加入 Zotero」；`need_fulltext` 的论文另有「上传全文」。

「去读」是链接，指向运行拾穗的那台电脑上的领读器，只在那台电脑上能打开；在手机上看到推送时用「稍后读」按钮，它通过回调记为待读。

### 11.4 公开和私有的边界

公开内容使用**字段白名单**（`PublicPaperView`），新增字段默认不公开：

| 字段 | 私有渠道 | 公开渠道 |
|---|---|---|
| 标题、作者、期刊、日期、DOI 链接 | ✅ | ✅ |
| 分数、等级、一句话总结 | ✅ | ✅ |
| 结论、不足、创新点（自己的概括） | ✅ | ✅ |
| 研究目标对照、反馈按钮 | ✅ | ❌ |
| 原文长引用、出版商摘要 | ✅ | ❌ 只放链接 |
| PDF、双语 PDF、归一化全文 | 不经渠道发送 | ❌ **永不公开** |
| 未设 `public: true` 的主题、反馈、费用 | ✅ | ❌ |

公开渠道收到的主题在选文时再过滤一次：即使路由写了 `*`，也只有 `public: true` 的主题能进入公开渠道。

LLM 输出来自不可信的论文内容：所有输出都限制长度；发给任何渠道前去掉其中的链接（裸域名不处理）；渲染时按纯文本处理，链接、@ 人和格式符号都不生效。公开渠道上线前要决定是否加人工审核。

### 11.5 不重复推送

- 同一路由、主题、周期只生成一份 Digest。
- **同一篇论文在同一渠道只推一次**，不分主题；卡片上列出它命中的所有主题。
- **例外：等级升级补推一次。** 以临时等级（`need_fulltext`）推过的论文，全文评估后进入更高的正式等级（如 `must_read`）时，再推一次并注明“读完全文后升级”。
- 发送前把状态记为 `sending`。适配器确定发送失败时抛 `TransientError` 或 `PermanentError`，可以重试。
- 其他异常（如超时、进程崩溃）无法确定对方是否收到：状态保持 `sending`，下次进入死信，**不自动重发**。确认后用 `litgleaner delivery resolve` 标记已收到或重发。
- 渠道支持幂等键时（如飞书消息的 uuid）一律带上。

**推送量受控**：路由的 `limit` 是上限。未读积压（`reading_state=queued` 超过阈值）时，每次推送按比例减少篇数，只保留 `must_read`，并在卡片上注明“你还有 N 篇待读”。推送频率默认每天一次，用户可以改为每周或暂停。

### 11.6 反馈回流

渠道按钮和转发的文件只写入 `feedback` 或 `inbox`，再由流水线读取。机器人不直接执行业务，所以它崩溃也不影响每日任务。

- 入站适配器放在各渠道自己的包里，例如飞书在 `adapters/feishu/listener.py`。适配器只负责核验身份（`answerers` 白名单）和格式，产出 `ports/inbound.CardClick`。
- 点击「加入 Zotero」写一条 `save_to_library` 反馈，由 `library` 阶段（`application/stages/library_save.py`）调用 `ReferenceLibrary` 写进文献库。
- 文献库没开时按 `Deferred` 推迟，不计失败次数。写成功后记 `library_entry`，重复点击不会重复写入。
- 飞书回调必须 3 秒内答复，所以监听只把点击放进队列，由服务进程的任务执行器处理，然后更新卡片。
- 飞书长连接由常驻服务维持。电脑休眠或关机时按钮点击收不到；需要随时可点的用户把服务部署到服务器（第 15.5 节）。
- 渠道配置步骤和验证层级见 [CHANNELS.md](CHANNELS.md)。

## 12. 领读器与 Zotero

领读器是拾穗自带的本地网页界面，由常驻服务提供（第 15.5 节）。它是用户每天打开的地方：首页是推荐收件箱和待读队列，点进一篇论文就进入阅读页。

### 12.1 “好”的标准

设计参照 Semantic Reader 项目经过用户研究验证的做法：就地释义（ScholarPhi）、面向非专家的章节要点（Paper Plain）、分面高亮帮助略读（Scim）、结合读者文库标注引用（CiteSee）。下面是领读器必须满足的原则，每条都要能验收：

| 原则 | 含义 | 验收方式 |
|---|---|---|
| 原文为中心 | 增强内容出现在原文旁边或悬停处，不替代原文；随时能一键回到纯原文 | 每个增强都可关闭；默认视图显示英文原文 |
| 每句都可核验 | 模型写的每条内容都能跳到原文位置；没有依据的标明“无原文依据” | 自动检查：展示的引用 100% 通过 `verify_quote`，未通过的不显示为原话 |
| 模型和作者分得清 | 作者原话、模型概括、用户笔记三种内容在视觉上明显不同 | 界面走查 |
| 术语一致 | 同一术语在译文、释义、卡片中用同一个译名 | 自动检查：译文中术语表译名的命中率 100% |
| 不打断阅读 | 侧栏不遮挡正文；增强按需加载，不阻塞翻页；常用操作有快捷键 | 缓存过的论文 2 秒内打开；段落翻译流式返回，首字 2 秒内出现 |
| 花费看得见 | 会产生模型费用的操作事先说明，并显示本月累计 | 界面走查 |
| 沉淀可回顾 | 批注、卡片、问答都存下来，可以检索，并能跳回原文 | 端到端测试 |

用户体验指标（任务完成时间、理解测试、可用性量表、信任问题次数）在真实用户测试中测量，流程和门槛见 [ROADMAP.md](ROADMAP.md)。

**交互准则**（依据见 [设计研究](experiments/2026-11-07-reader-design-research.md)）：

- 增强默认收起、按需展开；悬停卡只放一条释义加出处，不遮挡原文。
- 背景卡、章节导读、问题索引只做导航入口，每条都能跳到原文，不提供“读完卡片就够了”的路径。
- 分面高亮固定四色（目标、创新、方法、结果），约每段最多一句，只标高置信度的句子，每类可以单独关闭。
- 术语释义优先显示论文自己的定义和位置；术语有多种用法或没找到定义时明确说明。
- 参考文献标注只用“建议先读”“已在库”“已读”三种，读过或入库后自动降级。
- 不确定的内容用第一人称说出来（“我无法从文中判断”），并标为“模型推断”。
- 读后卡片默认“先写后看”：用户先写自己的判断，再展开模型的版本；可以关闭。
- 每条模型内容都能被用户标为有误；标记写入反馈，用于校准。
- 选中文字的菜单提供“连续高亮”模式，避免每次都弹菜单。
- 上线后监测来源锚点的点开率、“标为有误”的比例和“先写后看”的关闭率。

### 12.2 阅读页

| 区域 | 内容 |
|---|---|
| 正文 | **原版视图**（默认）：用 pdf.js 渲染原始 PDF，增强内容按页码和 `bbox` 叠加。**段落视图**：由 `NormalizedDocument` 重排，适合双语对照和小屏。两个视图之间按页码和块位置同步 |
| 读前 | **原理动画**（默认关闭）：用一个小例子分步演示论文的核心机制，每一步配原文引用<br>**背景卡**：这篇要解决什么、需要先懂的 3–5 个概念（每个都有释义和出处）、章节导读 |
| 读中 | **就地释义**：术语表中的词和论文自己定义的符号悬停显示释义<br>**分面高亮**：按 `extraction.highlights` 把目标、创新、方法、结果句分色标出，可以逐类开关<br>**按需翻译**：选中段落或点段落旁的按钮显示中文，可以切换为整篇双语对照<br>**边读边问**：针对选中内容或整篇提问<br>**引用标注**：参考文献标出“已在你的库里”“已读”“建议先读”及理由 |
| 读后 | **读后卡片**（第 9.4 节）：可编辑；可以导出为组会汇报提纲 |
| 侧栏 | 卡片、问答、批注三个标签页；批注支持高亮和文字笔记 |

- **原理动画默认关闭**：生成分镜要消耗较多 token，不同模型效果差距大。用户在设置中开启后，每篇论文仍需手动点“生成”，生成前显示预估费用。
  - **分镜**：模型只负责写分镜，以结构化数据存入 `explanation`（`kind = principle`），内容包括选哪个机制、几个步骤、每步说明、引用的原文、玩具例子的输入。引用按第 12.1 节核验。机制无法用小例子说明时（如纯定性的论文），不生成分镜。
  - **两种渲染方式**，在设置中选择：
    - `code`（推荐）：数值由代码按论文公式计算，前端用 HTML 或 SVG 逐步播放，可以暂停、单步、放大。
    - `image_gif`：把分镜交给 `ImageGenerator`（默认 GPT Image 系列）生成 GIF 动图，结果作为 `artifact` 缓存。图像模型可能画错文字、公式和数字，所以动图旁边固定标注“AI 生成动图，图中文字和数值未经核验，以原文为准”，并附上分镜的原文引用。动图不作为证据，不进入读后卡片和导出。
- 背景卡、章节导读和前置阅读指引在第一次打开时生成并缓存到 `explanation`。`must_read` 论文可以在推送前预生成，由配置决定。
- 解析质量为 `degraded` 时，段落视图顶部提示“结构识别不完整，以原版视图为准”。
- 中文论文也能打开：翻译和释义按目标语言自动跳过，其余功能照常。

### 12.3 翻译

原文为主，翻译按需进行，同时支持双语：

| 模式 | 说明 |
|---|---|
| 仅原文 | 默认 |
| 按需段落翻译 | 点哪段翻哪段；译文显示在原段下方或悬浮层 |
| 双语对照 | 段落视图中整篇逐段对照（上下或左右）；原版视图中译文作为侧栏对照 |
| 双语 PDF 导出（可选） | 通过 `Translator` 接口调用 BabelDOC 生成保留版式的双语 PDF；BabelDOC 由用户自行安装，不随安装包分发 |

- 段落翻译是 LLM 任务 `translate`，走统一网关，计入预算。
- 提示词中注入课题术语表；公式、符号、引用编号、表格按原样保留。
- 译文按 `(block, source_hash, target_lang, glossary_hash, translator_version)` 缓存，同一段只翻一次；术语表变化后不自动重译（第 7.1 节）。
- 整篇双语对照会先估算费用，用户确认后才执行。

### 12.4 边读边问

- 内置问答使用 `llm.yaml` 中的 `ask` 任务，不需要安装任何 agent。
- 上下文：选中内容、所在章节、相关 chunk（按需检索）、研究画像、读后卡片。只发送必要片段，不整篇发送。
- 回答必须给出引用的原文和页码，代码核验；没有依据的部分标为“模型推断”。
- 问答记录写入 `qa_message`，可以检索，也可以把一问一答存为批注。
- 进阶用户可以让 Claude Code、Codex 等外部 agent 通过只读 MCP（第 12.6 节）获取同样的上下文。

### 12.5 Zotero 同步（可选）

`ReferenceLibrary` 的 Zotero 实现走 Zotero 桌面端的本地 API（`127.0.0.1:23119`），不经过 zotero.org，**不需要安装任何 Zotero 插件**。

| 写入内容 | 说明 |
|---|---|
| 条目 | 有 arXiv 编号且没有期刊的建成预印本，其余建成期刊论文 |
| 分组和标签 | 默认分组「拾穗」；标签含主题名和推送等级 |
| 笔记 | 读后卡片和用户批注的导出（Markdown 转 HTML），末尾附拾穗编号 |
| PDF | 系统已经取到的全文；导出过双语 PDF 时一并挂上 |

- 查重：先按标题搜索，再核对 DOI 或 arXiv 编号；已有条目只补分组、标签和缺少的附件。
- 只写「我的文库」，不写群组文库。
- 授权：在领读器设置页点“连接 Zotero”，引导用户打开 Zotero 的本地 API 开关并在弹窗中点“始终允许”，本地 key 存入 `SecretStore`。
- 读取（种子分组、“已读”标签）优先用本地 API；不可用时读 `zotero.sqlite` 的只读副本，绝不直接打开或写入用户的数据库文件。
- 笔记同步是单向的（拾穗 → Zotero）。在 Zotero 里改的笔记不会回写，以免两边冲突。

### 12.6 给外部 agent 的只读接口

`litgleaner mcp` 以 stdio 方式提供只读 MCP 服务，Claude Code、Codex 等都可以挂载：

| 工具 | 返回 |
|---|---|
| `get_profile(topic)` | 研究背景、内容、目标、术语表 |
| `list_recommendations(topic, since)` | 推荐过的论文、分数和等级 |
| `get_paper(id 或 DOI)` | 元数据、各维度等级与证据、目标对照、读后卡片、用户批注 |
| `search_archive(query)` | 已归档论文的全文检索（FTS5），带页码 |
| `related_papers(paper)` | 同主题下方法、数据或引用相近的论文 |
| `comparison_table(topic)` | 第 14 节的方法对比表 |

- 只读：不提供任何写入或执行工具，不监听网络端口。
- 返回的论文内容用分隔标记包裹，并注明是不可信数据。
- 查询逻辑在 `application`，`mcp_server.py` 只做协议适配。

## 13. 待读队列与主题周报

### 13.1 待读队列

- 来源以领读器为主：点「稍后读」或在卡片上点「稍后读」记为 `queued`；打开论文记为 `reading` 并保存读到的位置；读后卡片确认完成或手动标记记为 `read`；点「不感兴趣」记为 `dropped`。
- 开启 Zotero 同步时，写入的条目打上待读标签（默认「待读」）；用户在 Zotero 里改成已读标签（默认「已读」）时，`zotero` 信号源下次运行读到后同步为 `read`。标签名在配置里设置，不写死。
- 领读器首页按“在读 → 待读 → 新推荐”排序，待读超过 N 天的必读论文置顶提醒。

### 13.2 主题周报

`kind: review` 的路由每周生成一份，按主题分节：

1. **待读提醒**：`queued` 超过 N 天的必读论文。
2. **本周进展**：只汇总本周已推送或已读的论文，按研究目标分组，说明新出现了什么方法、结论或数据集。
3. **回顾**：从已读论文中抽 2–3 篇，重现读后卡片的要点，对抗遗忘。

- 进展部分由 LLM 生成，输入只包含这些论文的提取结果和证据。每条陈述必须引用输入中的论文编号，代码校验编号存在于输入集合，不存在的陈述丢弃。
- 周报本身也是不可变的 Digest，走同样的渲染、发布和不重复推送规则。

## 14. 方法对比表与引用导出

```bash
litgleaner export table --topic example-topic --format md|csv [--tier must_read,digest]
litgleaner export refs  --topic example-topic --format bibtex|csl-json|gbt7714 [--read-only]
litgleaner export outline --topic example-topic   # 综述骨架
```

- **对比表**：每行一篇论文，列为方法、数据集、指标与结果、代码是否公开、阅读价值分、对应目标。每个单元格来自 `extraction`，可附页码；`unverified` 的内容标注出来。
- **引用导出**：只用信号源写入的元数据和标识符，LLM 不参与。只导出有 DOI 或 arXiv 编号的论文；缺少标识符的单独列出，标为“需要人工核对”。
- **综述骨架**：按研究目标或方法类别把已读论文分组成提纲，每组附对比表和可核验引用，并列出“研究空白候选”（对比表中没有被覆盖的方法与数据组合等），每条都指向具体论文。**不生成成段正文**。
- 对比表、引用、综述骨架都可以在领读器里查看和导出。
- 对比表和引用导出不花费 LLM 费用；综述骨架的分组说明由 LLM 生成，按需执行。

## 15. 运行与 LLM

### 15.1 任务调度

- 用 SQLite 的 `job` 表做任务队列，每个阶段自己定义幂等键。重复运行不会重复处理，也不会重复收费。
- 阶段顺序：discover → triage → acquire → parse → extract → assess → score → archive → select → publish → library。所有阶段都能单独运行：`litgleaner run --stage <name>`。
- 领读器的讲解、翻译、问答和双语 PDF 导出是按需任务，由用户操作触发，走同一个任务队列和预算；`must_read` 论文的背景卡可按配置在 `archive` 之后预生成。
- 同一时间只允许一个流程运行（文件锁），定时任务重叠时后者直接退出。
- 写入集中在常驻服务进程：流水线、渠道监听、领读器都在这个进程里，按任务排队执行，事务保持短小。命令行操作同样先取文件锁。
- `Deferred`（额度用完、外部任务仍在处理、Zotero 没开）推迟到指定时间再试，不计失败次数。
- 失败按指数退避重试，超过次数进入死信。`AuthFailed`、`SchemaChanged` 重试也不会成功，直接进入死信。有失败时 `litgleaner run` 退出码为 1。
- 解析或提取彻底失败时，论文退回按摘要评估并标记为临时分，不会从系统里消失。
- 安装版由常驻服务按配置的时间运行流水线；启动时发现错过了运行时间就补跑一次。服务器部署也可以由 cron 调用 `litgleaner run`。

### 15.2 LLM

- 按任务配置模型，`providers` 定义怎么连（协议、地址、密钥、价格），`tasks` 定义哪个任务用哪个模型：

```yaml
tasks:
  triage:      { provider: main, model: CHEAP_MODEL,  prompt: triage }
  extract:     { provider: main, model: STRONG_MODEL, prompt: extract }
  assess:      { provider: main, model: STRONG_MODEL, prompt: assess }
  topic_draft: { provider: main, model: STRONG_MODEL, prompt: topic_draft }
  review:      { provider: main, model: STRONG_MODEL, prompt: review }
  explain:     { provider: main, model: STRONG_MODEL, prompt: explain }     # 背景卡、章节导读、前置阅读
  translate:   { provider: main, model: CHEAP_MODEL,  prompt: translate }
  principle:   { provider: main, model: STRONG_MODEL, prompt: principle, enabled: false }   # 原理动画分镜，默认关闭
  ask:         { provider: main, model: STRONG_MODEL, prompt: ask }
budget:
  daily_usd: 2.0
```

- 预算用完后 LLM 任务保持待处理，第二天继续，不计失败次数；积压时**优先处理最新发现的论文**。
- 领读器中用户主动发起的操作（翻译、提问、讲解）优先于后台任务；预算用完时提示用户，可以临时追加当天额度。
- 支持国内低价模型（OpenAI 兼容接口）；领读器设置页显示每篇论文和本月的花费。
- 输出必须通过 JSON Schema 校验，失败只重试有限次。
- `assess` 提示词根据当前 rubric 和研究画像动态生成，维度不写死。
- **论文内容一律视为不可信数据**，用分隔标记包裹，提示词明确其中的指令无效。模型输出只允许填写固定字段，总分由代码计算，模型没有任何工具权限。

### 15.3 数据去向

拾穗不区分内容敏感程度，下表说明每类数据会发到哪里，供使用者自行判断：

| 数据 | 发往 | 何时 |
|---|---|---|
| 检索词、DOI、联系邮箱 | 已启用的信号源和全文来源 API | 发现、取全文 |
| PDF 文件 | MinerU 云端（使用 `mineru_cloud` 时） | 解析 |
| 标题、摘要、全文片段、研究画像、术语表 | `llm.yaml` 中配置的模型提供方 | 初筛、提取、评估、起草检索式、周报、领读器的讲解、翻译和问答 |
| 用户在领读器中的提问和选中内容 | 同上 | 边读边问 |
| PDF 全文 | BabelDOC 使用的模型提供方 | 导出双语 PDF（可选） |
| 原理动画的分镜（步骤说明、原文短引用） | 配置的图像模型提供方 | 用户开启并选择 `image_gif` 渲染时 |
| 卡片内容（标题、分数、理由、短引用） | 飞书等推送渠道 | 推送 |
| 条目、笔记、PDF | 本机 Zotero，不经过 zotero.org | 加入文献库 |
| Zotero 文库条目 | api.zotero.org（仅 Web 模式） | 读取收件箱和全文 |
| MCP 查询结果 | 调用它的 agent，以及该 agent 使用的模型提供方 | agent 查询 |

### 15.4 密钥

密钥通过 `SecretStore` 读取，配置文件里只存密钥名（`credentials_env`、`optional_env`）。启动时检查已启用组件所需的密钥，日志、界面和命令输出不显示密钥。

- 安装版：在向导或设置页填写，存入系统钥匙串；领读器只显示“已设置”和末四位。
- 命令行和服务器：从环境变量读取；服务器上存入权限为 600 的环境文件，不进 git。环境变量优先于钥匙串。
- Token 会过期：适配器收到认证失败时抛 `AuthFailed` 并告警，不计入重试。

### 15.5 发布与运行形态

| 形态 | 面向 | 内容 |
|---|---|---|
| 桌面安装包（默认） | 普通用户 | macOS `.dmg`、Windows 安装程序；内含 Python 运行时、依赖和构建好的前端；双击启动常驻服务并打开浏览器 |
| `uv tool install litgleaner` | 会用命令行的用户 | `litgleaner serve` 启动同一个服务 |
| 容器镜像 | 需要全天在线（飞书按钮随时可点）的用户 | 部署到服务器，通过 SSH 隧道或反向代理访问领读器 |

- **常驻服务**（`service.py`）在一个进程里组合：领读器 HTTP 接口、定时调度、飞书长连接、任务执行器。可以在设置中开启登录时自动启动（macOS LaunchAgent、Windows 开机启动项、Linux systemd 用户服务）。
- **本地安全**：只监听 `127.0.0.1` 的随机端口；每次启动生成访问令牌，浏览器通过启动链接获得会话；检查 `Host` 和 `Origin` 头，防止 DNS 重绑定和跨站请求；改变状态的请求带 CSRF 令牌；设置严格的 CSP；模型输出一律按纯文本或经过清洗的 Markdown 渲染。
- **首次向导**：① 选模型提供方（推荐低价模型）并填 key，测试连接并估算月花费；② 用中文写研究背景、内容、目标，起草检索式和术语表并确认；③ 飞书（图文指引，可以跳过）；④ Zotero（自动检测，可以跳过）。
- **密钥**：安装版通过 `SecretStore` 存入系统钥匙串（macOS 钥匙串、Windows 凭据管理器）；命令行和服务器部署继续用环境变量。配置文件里只存密钥名。
- **升级**：启动时检查新版本并提示，不自动安装；升级后先自动备份，再执行数据库迁移。
- **代码签名**：macOS 发布包需要 Developer ID 签名和公证，Windows 发布包需要代码签名证书，否则系统会拦截或警告。

## 16. 防止代码腐化的机制

规则要靠工具执行，不靠自觉：

| 风险 | 机制 |
|---|---|
| 依赖方向被破坏 | `import-linter` 契约 |
| 流程代码出现具体厂商名 | `tests/architecture/`：`application`、`ports` 中出现信号源、解析器、大模型厂商、飞书、Zotero、BabelDOC 等名字就失败 |
| 适配器行为不一致 | `tests/contracts/` 统一契约测试，新适配器必须通过 |
| 外部 API 变化不被发现 | 录制响应做回放测试；线上 `SchemaChanged` 告警 |
| 配置拼写错误或默默失效 | Pydantic `extra="forbid"`，`litgleaner doctor` 启动自检 |
| 数据库结构漂移 | Alembic 迁移，禁止手动改表 |
| 数据被意外修改 | 领域模型不可变（frozen），`feedback` 等记录只追加 |
| 文件和函数膨胀 | 文件不超过 400 行，函数不超过 50 行（`tests/architecture/` 检查） |
| 决策失传 | 本文档 + `docs/adr/` |
| 回归 | 单元、契约、集成、E2E 四层测试，覆盖率不低于 80%；第 17 节的变更场景写成 E2E 测试 |

**故意不做**（以及什么时候才做）：

| 不做 | 原因 | 触发条件 |
|---|---|---|
| 微服务、消息队列 | 单人单机，SQLite 队列足够 | 需要多机并发处理 |
| PostgreSQL | 写入集中在一个服务进程，SQLite 足够 | 需要多用户共享或多机写入 |
| 向量数据库 | FTS5 足够支撑文库检索 | 关键词检索明显不够用 |
| 动态插件加载 | 显式注册表更容易调试 | 需要第三方安装插件 |
| 公开的网页服务 | 领读器只在本机 `127.0.0.1` 提供，数据不出本机 | 要做登录、评论或多人协作 |
| 支持多种数据库 | `ports/store.py` 的接口只用来维持依赖方向 | 出现第二种实现 |
| 给切分器抽象接口 | 只有一种实现 | 出现第二种实现 |
| MCP 写入工具 | 写入要经过反馈和调度，保证幂等和可追溯 | 有明确的 agent 写入需求，并先设计权限 |

## 17. 用变更场景检验架构

| 场景 | 需要改动的地方 | 不用动的部分 |
|---|---|---|
| 新增 Scopus 信号源 | 1 个适配器、`bootstrap.py` 注册表、`sources.yaml`、录制数据 | 流程、评分、推送 |
| 下线某信号源 | `sources.yaml` 一行 | 其他全部 |
| 调权重 | rubric JSON，执行 `rescore` | 代码；不产生 LLM 费用 |
| 新增评价维度 | rubric JSON，执行 `reassess` | 代码 |
| 新增研究目标 | 主题 YAML 的 `goals`，执行 `activate`，按需 `backfill` | 已有 PDF、解析结果、提取内容 |
| 新建研究画像 | 主题 YAML，`topic draft` 起草检索式，`activate` | 代码 |
| 换成本地 MinerU | 新增解析适配器，改配置 | 切分、提取、评估 |
| 换翻译模型 | `llm.yaml` 中 `translate` 任务 | 领读器；旧译文按指纹保留，不自动重译 |
| 换双语 PDF 工具 | 新增 `Translator` 适配器，改配置 | 领读器、调度 |
| 领读器增加一种讲解（如“方法图解”） | `application` 加生成函数和 `explanation.kind`，`server/` 加接口，`web/` 加组件 | 流水线、数据库结构 |
| 改读后卡片字段 | 提取的结构和提示词，`extractor_version` 随之变化 | 评分；旧卡片由 `stale` 报告列出 |
| 支持 EndNote 等其他文献库 | 新增 `ReferenceLibrary` 适配器，改配置 | 推送、反馈、调度 |
| 新增邮件或 Telegram 推送 | 新增 Publisher、Renderer 和入站适配器，改配置 | 选文和 Digest |
| 给 agent 增加一个查询 | `application` 加查询函数，`mcp_server.py` 注册工具 | 流水线 |
| 换大模型 | `llm.yaml`，必要时加协议适配器 | 流程；只影响新论文，旧结果要重做时显式 `reassess` |
| 文件迁到对象存储 | 新增 `BlobStore` 适配器，迁移数据，改配置 | 业务代码 |
| 解析器输出格式变化 | 该解析适配器的转换函数 | 下游全部 |
