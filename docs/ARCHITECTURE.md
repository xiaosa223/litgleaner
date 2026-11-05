# 论文工厂架构设计

- 状态：第 1 步（行走骨架）已实现，外部依赖目前都是假适配器
- 日期：2026-11-03
- 包名：`litfactory`（暂定）

本文档是实现的依据。要改变这里的决策，先改文档或新增 `docs/adr/` 记录，再改代码。

## 1. 目标与边界

**目标**：每天从多个信号源发现文献，按研究主题筛选和评分，把值得读的归档并推送到 Telegram、网页等渠道。

**必须容易变的东西**：

| 变化点 | 改动方式 | 不应该发生的事 |
|---|---|---|
| 增减信号源 | 加一个适配器和一段配置，或把 `enabled` 改成 `false` | 改流程代码 |
| 改评价指标 | 改一个 JSON 文件 | 改代码；新旧分数混在一起比较 |
| 切换研究主题 | 改一个主题文件并执行 `activate` | 重新下载、重新解析已有文献 |
| 增减推送渠道 | 加一个发布适配器和路由配置 | 评分或选文代码感知渠道 |
| 换解析器（云端↔本地） | 换适配器 | 切分和评估代码感知 MinerU 格式 |
| 换存储位置（本地↔R2） | 改配置 | 业务代码出现文件路径 |

## 2. 三条核心原则

1. **事实与判断分离。** 论文的元数据、全文、提取内容是事实，全局只存一份。相关性、分数、是否推送是判断，按“主题 + 评价标准”分别存放。这是切换主题和修改指标不用重来的根本原因。
2. **一切派生物都带来源指纹。** 每个派生结果都记录“输入哈希 + 生产者版本”。配置变了，系统能算出哪些结果失效，而不是全量重跑。
3. **变化点走接口，其余保持简单。** 只给已知会有多种实现的地方定义接口，其他地方不做抽象（见第 13 节）。

## 3. 总体流程

```text
 配置: topics / rubrics / sources / channels / retention
                          │
 ┌──────────── 全局：事实层（每篇只做一次）───────────┐
 │ discover → resolve → enrich → acquire → parse → extract │
 │  发现       去重合并   补元数据   取全文   归一化    提取事实 │
 └─────────────────────────────────────────────────┘
        │ 新论文                    ▲ 初筛通过才触发取全文
        ▼                          │
 ┌──────────── 按主题：判断层 ───────────────────────┐
 │ triage ───────────┘    assess → score → select      │
 │ 初筛相关性              分维度评估  算分    选文         │
 └─────────────────────────────────────────────────┘
                          │
 ┌──────────── 输出层 ───────────────────────────────┐
 │ Digest → 可见性投影 → 渲染 → 发布(outbox) → 渠道       │
 │                                   Telegram / 网页 / …  │
 └─────────────────────────────────────────────────┘
                          │
 反馈（按钮、手动投喂）→ inbox → 回流为信号源和校准数据
 审计 audit：覆盖率、漏检、误杀抽查、渠道健康（横切全流程）
```

初筛通过后才取全文和解析，这是控制成本和存储的主要手段。

## 4. 模块与依赖规则

```text
论文工厂/
├─ config/
│  ├─ app.yaml  sources.yaml  channels.yaml  llm.yaml  retention.yaml
│  ├─ journals/<set>.yaml        # 期刊清单，可被多个主题引用
│  ├─ topics/<topic-id>.yaml     # 一个主题一个文件
│  └─ rubrics/<rubric-id>.json   # 评价指标，唯一来源
├─ prompts/                      # 提示词是文件，不写在代码里
├─ src/litfactory/
│  ├─ domain/       # 模型与纯逻辑：Paper、Rubric 算分、切分器、去重规则
│  ├─ ports/        # 接口：Source、FullTextProvider、Parser、LLM、Publisher、BlobStore
│  ├─ application/  # 各阶段、任务调度、选文、审计
│  ├─ adapters/     # 外部实现：sources/ fulltext/ parsers/ llm/ publishers/ blobstore/ inbound/
│  ├─ infra/        # 数据库、迁移、配置加载、HTTP 客户端
│  ├─ bootstrap.py  # 组装程序，含适配器注册表（显式字典）
│  └─ cli.py
├─ tests/  unit/ contracts/ integration/ e2e/ fixtures/
├─ archive/          # JSON 归档（进 git，私有）
├─ data/             # 数据库、PDF、解析结果（gitignore）
└─ docs/  ARCHITECTURE.md  adr/
```

**依赖方向只能向内**，由 `import-linter` 在 CI 中强制：

```text
cli → bootstrap → adapters, infra → application → ports → domain
```

- `domain` 不依赖任何外部库的 IO，不知道 MinerU、Telegram、SQLite 的存在。
- 适配器之间不能互相导入，也不能访问 `application` 和 `infra`。
- 只有 `adapters/` 里可以出现具体来源名。`application` 中写 `if source == "ieee"` 视为缺陷。

## 5. 数据模型

存储用 SQLite（WAL 模式）+ SQLAlchemy Core + Alembic，从第一天就有迁移。

| 层 | 表 | 主键或唯一键 | 说明 |
|---|---|---|---|
| 事实 | `paper` | 内部 ULID | 标题、摘要、作者、期刊、日期 |
| 事实 | `paper_alias` | `(scheme, value)` | DOI、arXiv ID、IEEE 编号都映射到同一篇 |
| 事实 | `merge_log` | 自增 | 合并记录，支持撤销 |
| 事实 | `sighting` | `(paper, source, query_hash)` | 谁在何时发现了它，覆盖率统计的基础 |
| 事实 | `source_cursor` | `(source, query_hash)` | 增量抓取水位 |
| 事实 | `artifact` | `sha256 + kind` | PDF、归一化文档、解析器原始输出等文件的索引和保留策略 |
| 事实 | `fulltext` | `paper` | 全文状态：`available`、`parsed`、`unavailable`、`failed` |
| 事实 | `extraction` | `(paper, input_hash, extractor_ver)` | 结论、不足、创新点及证据 |
| 判断 | `topic_state` | `topic` | 主题是否启用、当前定义的哈希、从何时开始初筛 |
| 判断 | `triage` | `(paper, topic, topic_hash)` | 初筛结果和理由，提示词版本只作记录 |
| 判断 | `assessment` | `(paper, topic, semantic_hash, input_hash)` | 各维度等级和证据，由 LLM 产生 |
| 判断 | `score` | `(assessment, policy_hash)` | 最终分和等级，由代码算出 |
| 判断 | `rubric_snapshot` | `hash` | 首次使用时保存指标全文，历史分数可追溯 |
| 输出 | `digest` / `digest_item` | ULID | 一次推送的不可变内容 |
| 输出 | `delivery` | `(digest, channel)` | 发送状态，保证不重复推送 |
| 反馈 | `feedback` | ULID | 只追加：有用、没用、已读、需要全文（第 4 步加入） |
| 反馈 | `inbox` | ULID | 手动投喂和邮件待处理项（第 3 步加入） |
| 审计 | `miss_log` | ULID | 漏检记录及原因（第 5 步加入） |
| 运行 | `job` | 幂等键 | 任务队列、重试、死信 |
| 运行 | `llm_call` | ULID | 模型、token、费用，用于预算控制 |

**去重规则**：标识符完全相同才自动合并；标题加作者的模糊匹配必须高置信才合并，否则进入待确认。所有合并都写 `merge_log`。目前只会把新标识符挂到已有论文上，发现两篇已有论文互相冲突时只记录、不合并；撤销和人工确认的命令在真正出现这类情况后再做。同一研究的预印本和正式版是同一 `paper` 的不同版本，正式版出现后可以触发重新评估。

## 6. 信号源

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
| `JournalWatch` | Crossref、IEEE RSS |
| `KeywordSearch` | IEEE API、WoS、arXiv |
| `CitationWatch`、`AuthorWatch` | Semantic Scholar、OpenAlex |
| `InboxDrain` | 邮件提醒、Telegram 手动投喂 |

### 6.2 配置

```yaml
sources:
  crossref:
    adapter: crossref
    enabled: true
    options: { rate_limit_per_sec: 1 }
  ieee:
    adapter: ieee_xplore
    enabled: true
    credentials_env: [IEEE_API_KEY]
  wos:
    adapter: wos_starter
    enabled: false
    credentials_env: [WOS_API_KEY]
    options: { daily_request_budget: 50 }
```

### 6.3 规则

- **加来源**：写一个适配器文件，在 `bootstrap.py` 的注册表里加一行，加配置，通过 `tests/contracts/` 的统一测试。
- **减来源**：`enabled: false`。历史 `sighting` 保留，审计时仍能看到它的贡献。
- **原始响应先落盘**，再解析。解析器修好后可以重放，不必重新抓取。
- 限流、重试、退避由共享 HTTP 客户端负责，适配器不各写一套。
- 同一个 API 的元数据发现和全文获取是两个独立适配器，分别实现 `Source` 和 `FullTextProvider`。

### 6.4 全文获取

`FullTextProvider` 按配置顺序依次尝试：预印本 → Unpaywall → 出版商 API → 人工补全（Telegram 转发）→ 仅摘要。每个 `artifact` 记录 `license_basis`（`open` / `subscribed` / `user_supplied`），决定保留策略，并保证这些文件不会被公开。不实现爬取出版商网站或 Sci-Hub 适配器。

## 7. 研究主题

一个主题一个文件，切换主题不需要改代码，也允许多个主题同时运行。文件只存定义，是否启用记录在数据库（`topic_state`），由命令切换：

```yaml
id: example-topic
name: 示例主题
description: >
  用自然语言描述研究问题、关心什么、不关心什么。LLM 判断相关性时使用。
query:
  include: ["keyword a", "keyword b"]
  synonyms:
    keyword a: ["别名 1", "别名 2"]
  exclude: ["不相关的词"]
journal_sets: [core]
seeds:
  dois: ["10.1109/EXAMPLE.2026.0000001"]
  authors: []
rubric: default
public: false             # 是否允许出现在公开网页
```

- `topic_hash` 只计算影响判断的字段（`description`、`query`）。这些字段变化后，**只有新发现的论文按新定义处理**，运行时会提示执行 `backfill`。旧定义下的判断不再参与排序，但不会自动花钱重筛历史。
- 新启用的主题同样只处理之后发现的论文，历史论文必须显式 `backfill`。
- `seeds` 同时用于引文追踪和校准集。
- 切换流程：

```bash
litfactory topic activate example-topic
litfactory topic backfill example-topic --since 2024-01-01             # 默认只预览数量和费用
litfactory topic backfill example-topic --since 2024-01-01 --execute   # 确认执行
```

`backfill` 目前用库内已有元数据重新初筛（只花初筛费用），向信号源补查历史要等真实适配器接入后实现。已有的 PDF、解析结果和提取内容直接复用。暂停的主题不消耗资源，数据全部保留。

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
      "question": "这篇论文和当前研究主题有多接近？",
      "anchors": { "0": "完全无关", "3": "同领域，可借鉴", "5": "直接解决主题的核心问题" }
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
reading_value = round( Σ(weight × level / levels.max) / Σ(weight) × 100 )
```

- **未通过门槛**：直接归入 `drop`。
- **无法判断**：等级为 `null`，只按可判断维度的权重计算，并标记 `provisional`。
- **只有摘要时**：`requires_fulltext` 的维度为 `null`，结果标记 `provisional`。这类论文不能进入 `must_read`；达到 `provisional.min_score` 时归入 `need_fulltext`，推送时附“需要全文”按钮。
- 启动时校验：权重总和为 100、等级和门槛合法、`tiers` 完整。校验失败直接退出，不带错运行。

### 8.3 修改指标的两种情况

直接改 JSON，git 记录历史，系统保存快照。系统用两个哈希判断影响范围：

| 修改内容 | 变化的哈希 | 处理方式 | 费用 |
|---|---|---|---|
| 权重、门槛、等级线 | `policy_hash` | `litfactory rubric rescore`，用已有维度等级重算 | 零 LLM 费用 |
| 增删维度、问题、锚点 | `semantic_hash` | `litfactory rubric reassess <主题>` 先预览，加 `--execute` 按范围重评 | 有 LLM 费用 |

只有 `semantic_hash` 和 `policy_hash` 都相同的分数才能直接比较。选文只使用当前指标下的分数，没有重算的旧分数不参与排序。

**任何配置修改都不会静默产生费用**：改维度定义、主题定义或提示词后，系统不会自动重评已有论文，新论文才用新配置。已归档的论文也不会因此被删除，文件里保留旧结果并标记 `stale: true`。只有在当前标准下明确判定为不保留时，归档才会被移除。

`litfactory rubric eval`（第 4 步实现）用校准集和反馈数据比较修改前后的效果（前 K 篇命中率、必读漏选率）。改指标先看这个结果，不凭感觉。

## 9. 文献处理：解析与切分

### 9.1 防腐层：统一文档格式

MinerU 云端和本地版本的输出结构不同，版本升级也可能变化，所以每个解析适配器都必须转换成自己的 `NormalizedDocument`。下游只认这个格式：

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

默认使用精准 API 的 `vlm` 模型（适配器 `mineru_cloud`），不使用 Agent 轻量接口：后者只返回 Markdown，没有页码、坐标和结构 JSON，无法生成上面的格式。

**MinerU 云端字段映射**（来自实测的 `*_content_list.json`：IEEE 双栏、NeurIPS 单栏、ACM 双栏、中文期刊各一篇，见 `docs/experiments/2026-11-05-parser-comparison.md`）：

| MinerU `type` | 关键字段 | 归一化处理 |
|---|---|---|
| `text` | `text`、`text_level`、`bbox`、`page_idx` | `text_level` 非空则为 heading，否则为 text |
| `equation` | `text`（LaTeX）、`text_format` | equation |
| `table` | `table_body`（HTML）、`table_caption`、`table_footnote` | table |
| `chart` | `img_path`、`chart_caption`、`chart_footnote`、`content` | figure |
| `code` | `code_body`、`code_caption`、`sub_type` | code |
| `ref_text` | `text` | reference |
| `page_footnote` | `text` | 写入 `front_matter` |
| `image` | `img_path`、`image_caption`、`image_footnote`、`content` | figure |
| `page_number`、`aside_text`、`header`、`footer` | `text` | 丢弃 |

样本中没有出现的类型（比如 `list`）不能静默丢弃：保留文字并把文档标记为 `degraded`。`header`、`footer`、`image` 就是这个机制在新样本中发现的。

### 9.2 切分规则

切分器是 `domain/chunker.py` 的纯函数：相同输入和版本得到完全相同的结果。

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

每个 chunk 的字段：`chunk_id`、`section_type`、`section_path`、`page_start`、`page_end`、`token_count`、`text`。这些参数放在配置里，修改后 `chunker_version` 随之变化。

**标题识别不到时不能静默出错**：先用规则匹配，缺少结论章节时取参考文献前的最后两节并标记 `degraded`。低质量解析结果会反映在推送内容里。

实测输出决定了以下规则，切分器必须针对它们写测试：

- **标题可能带空格**：小型大写会被拆成 `I NTRODUCTION`，中文标题常写成 `5 总 结`、`参　考　文　献`，分类时也要检查去掉空格的写法。
- **中文期刊有双语前置部分**：页首的英文标题不算正文开始；`Abstract In ...`（没有破折号）、`Key words`、`摘　要`、`结束语` 都要识别。
- **参考文献后可能有作者简介和照片**：解析器标出了参考文献条目时只用这些条目。
- **`text_level` 不能表示层级**：样本中 `V. CONCLUSION` 和 `A. Case Study` 都是 2 级，章节嵌套要靠编号（`I.`、`A.`、`1.1`）推断。
- **摘要不是标题**：IEEE 论文的 `Abstract—...`、`Index Terms—...` 是普通段落，要按段首模式识别。如果只找标题，IEEE 论文就没有摘要。
- **论文标题以元数据为准**：解析结果里的 1 级标题只作校验。
- **图注关联不可靠**：样本把 Fig.10 的图注挂到了 Fig.11(a)，又把 Fig.11 的图注混入表 III；26 个分图只有 19 个带图注。评估不能依赖“图和图注一定对应”。
- **解析文本不等于原文**：样本有箭头缺失、向量 `d` 被识别成 `1`、符号串码等错误。引用校验只能证明“引文存在于解析文本”，所以推送中的公式和关键数字要提示以原文为准。

### 9.3 切分的用途

1. **节省 LLM 费用**：提取和评估只读摘要、引言、方法概述、结果、讨论、结论，并设总 token 上限。
2. **证据可追溯**：提取结果记录原文引用和页码，代码校验引用确实存在于原文；校验失败标记为 `unverified`，不当作事实。
3. **文库检索**：只有归档论文的 chunk 写入 SQLite FTS5。

证据里保存引用原文和页码，不只存 `chunk_id`，这样切分规则升级后旧证据依然有效。

语义向量检索现在不做，等真正需要“问文库”时再加，届时从归一化文档直接生成，不需要重新解析 PDF。

## 10. 存储

### 10.1 放在哪里

| 内容 | 位置 | 进 git | 能否重建 |
|---|---|---|---|
| 配置、提示词、代码 | 仓库 | 是 | — |
| JSON 归档 | `archive/papers/YYYY/MM/<paper_id>.json` | 是（私有仓库） | 能，由数据库导出 |
| 数据库 | `data/db/litfactory.db` | 否，每日备份 | 否，**最重要的数据** |
| 原始 API 响应 | `blob://raw/<source>/YYYY/MM/DD.jsonl.zst` | 否 | 大多能，但会过期 |
| PDF | `blob://pdf/ab/cd/<sha256>.pdf` | 否 | 开放文献能，付费文献不一定 |
| 归一化文档 | `blob://doc/ab/cd/<sha256>.<parser>.json.zst` | 否 | 能，但消耗解析额度 |
| 解析器原始输出 | `blob://parser_raw/<sha256>.content_list.json.zst`（只留这一个文件） | 否 | 能 |
| chunk | 不单独存，需要时计算；归档论文写入 FTS | 否 | 能，纯函数 |

**真相只有一处**：SQLite 是运行时的唯一真相，JSON 归档是由 `archive` 阶段生成的导出结果，永远不手改、不反向写入。

业务代码只使用 `blob://` 地址和 `BlobStore` 接口，因此可以把文件从本地迁到 R2，而不改业务代码。

### 10.2 如何减少存储

1. **漏斗**：初筛没通过的论文不下载 PDF，只存元数据和摘要。这是效果最大的一项。
2. **内容寻址**：文件以 SHA-256 命名，相同 PDF 只存一份。
3. **压缩**：文本和 JSON 用 zstd 压缩。
4. **解析后瘦身**：ZIP 解压后立即丢弃 `layout.json`、`model.json`、`full.html`、`full.tex`、`full.docx`、`origin.pdf` 和缩略图，`content_list.json` 只留 30 天。实测一篇 8 页论文的结果目录为 7.1MB，其中 `layout.json` 约 2MB，而 `content_list.json` gzip 后只有约 18KB；缩略图长边仅 317～523 像素，需要配图时按 `bbox` 从 PDF 恢复高清原图。
5. **按价值分级保留**（`retention.yaml`）：

| 论文状态 | 元数据和摘要 | 提取结果 | 归一化文档 | PDF |
|---|---|---|---|---|
| 初筛淘汰 | 永久 | 无 | 无 | 从未下载 |
| 深评 `drop` | 永久 | 永久 | 90 天 | 30 天 |
| 深评 `digest` | 永久 | 永久 | 永久 | 180 天 |
| `must_read` 或用户收藏 | 永久 | 永久 | 永久 | 永久 |
| 用户上传或付费获取且未归档 | 永久 | 永久 | 90 天 | 90 天 |

元数据和摘要永久保留，切换主题时才有东西可以重新初筛；提取结果体积很小，也永久保留。清理（第 4 步实现）由 `litfactory gc --dry-run` 预览后执行，并且只有 `job` 全部完成后才能删除对应文件。

### 10.3 容量估算

以下是**假设值，不是实测值**：每天 500 篇候选，每条元数据 3KB；20 篇深评，每篇文档 50KB；5 篇长期保留 PDF，每篇 3MB。

| 项目 | 每年 |
|---|---|
| 元数据 | 约 0.55GB |
| 归一化文档 | 约 0.37GB |
| 长期保留的 PDF | 约 5.5GB |
| **合计** | **约 6.4GB** |
| 对比：每篇候选都存 PDF | 约 550GB |

上线后 `litfactory stats` 报告真实数据（目前只有条数，文件大小统计在第 4 步加入）。

### 10.4 备份

每日执行 `VACUUM INTO` 生成数据库快照并上传对象存储。配置和归档由 git 保存。必须定期做恢复演练，否则备份不算数。

## 11. 发布

### 11.1 分层

```text
Selector（按路由选文）
  → Digest（不可变内容，存库）
  → 可见性投影（private / public）
  → Renderer（Telegram HTML / 网页 JSON）
  → Publisher（只负责发送）
  → delivery（记录状态，失败重试，不重复发送）
```

选文和内容与渠道完全无关，一份 Digest 可以发到多个渠道。

```python
class Publisher(Protocol):
    name: str
    visibility: Literal["private", "public"]

    def publish(self, rendered: Rendered) -> Receipt: ...
```

### 11.2 配置：渠道和路由分开

```yaml
channels:
  telegram_me:
    adapter: telegram
    renderer: telegram_html
    visibility: private
    credentials_env: [TELEGRAM_BOT_TOKEN, TELEGRAM_CHAT_ID]
  website:
    adapter: static_site
    renderer: site_json
    visibility: public
    enabled: false

routes:
  - { id: daily-me, topic: "*", channel: telegram_me, schedule: "daily 08:30", tiers: [must_read, need_fulltext], limit: 5 }
  - { id: weekly-me, topic: "*", channel: telegram_me, schedule: "weekly mon 09:00", tiers: [digest], limit: 15 }
  - { id: daily-web, topic: public_topics, channel: website, schedule: "daily 09:00", tiers: [must_read, digest], limit: 50 }
```

### 11.3 公开和私有的边界

公开内容使用**字段白名单**（`PublicPaperView`），新增字段默认不公开：

| 字段 | Telegram（私有） | 网页（公开） |
|---|---|---|
| 标题、作者、期刊、日期、DOI 链接 | ✅ | ✅ |
| 分数、等级、自己生成的一句话总结 | ✅ | ✅ |
| 结论、不足、创新点（自己的概括） | ✅ | ✅ |
| 反馈按钮和“需要全文”操作 | ✅ | ❌ |
| 原文长引用、出版商摘要 | ✅ | ❌ 只放链接 |
| PDF、归一化全文 | 需要时私发 | ❌ **永不公开** |
| 未设 `public: true` 的主题、反馈、费用 | ✅ | ❌ |

公开渠道收到的主题还会在选文时再过滤一次：即使路由写了 `*`，也只有 `public: true` 的主题能进入公开渠道。

### 11.4 不重复推送

- 同一路由、主题、周期只生成一份 Digest，已推送到某渠道的论文不会再进入该渠道。
- 发送前把状态记为 `sending`。适配器确定发送失败时抛 `TransientError` 或 `PermanentError`，可以重试。
- 其他异常（比如超时、进程崩溃）无法确定对方是否收到，状态保持 `sending`，下次进入死信，**不自动重发**。确认后用 `litfactory delivery resolve` 标记已收到或重发。

### 11.5 网页方案

先做**静态站点**：`static_site` 适配器生成 JSON 和页面，部署到 Cloudflare Pages。网站不连数据库、没有后端，所以 PDF 和私有数据不存在泄露路径。以后要评论或登录，再单独加服务，不影响流水线。

LLM 输出来自不可信的论文内容：所有输出都限制长度，发给任何渠道前都会去掉其中的链接（裸域名不处理），渲染时必须转义。上线公开网页前，还要决定是否加人工审核，见第 16 节。

### 11.6 反馈回流

入站是独立模块（`adapters/inbound/`）。Telegram 按钮和转发的文件只写入 `feedback` 或 `inbox`，流水线再读取。机器人不直接触发流程，崩溃也不影响每日任务。网页以后加反馈时，走同一个入口。

## 12. 运行与 LLM

### 12.1 任务调度

- 用 SQLite 的 `job` 表做任务队列，每个阶段自己定义幂等键。重复运行不会重复处理，也不会重复收费。
- 同一时间只允许一个流程运行（文件锁），定时任务重叠时后者直接退出。
- `Deferred`（额度用完、外部任务仍在处理）推迟到指定时间再试，不计失败次数。
- 失败按指数退避重试，超过次数进入死信。`AuthFailed`、`SchemaChanged` 重试也不会成功，直接进入死信。有失败时 `litfactory run` 退出码为 1，主动告警在第 4 步加入。
- 解析或提取彻底失败时，论文退回按摘要评估并标记为临时分，不会从系统里消失。
- 由 cron 或 launchd 调用 `litfactory run`，运行位置无关：VPS 或 Mac 只差配置。
- 所有阶段都能单独运行：`litfactory run --stage parse`。

### 12.2 LLM

- 按任务配置模型，初筛用便宜模型，深评用强模型：

```yaml
tasks:
  triage: { provider: main, model: CHEAP_MODEL, prompt: triage }
  extract: { provider: main, model: STRONG_MODEL, prompt: extract }
  assess: { provider: main, model: STRONG_MODEL, prompt: assess }
budget:
  daily_usd: 2.0   # 用完后 LLM 任务保持待处理，第二天继续，不计失败次数
```

- 输出必须通过 JSON Schema 校验，失败只重试有限次。
- `assess` 提示词根据当前 rubric 动态生成，维度不写死。
- **论文内容一律视为不可信数据**，用分隔标记包裹，提示词明确其中的指令无效。模型输出只允许填写固定字段，总分由代码计算，模型没有任何工具权限。

### 12.3 密钥

程序只从环境变量读取，配置文件里只存变量名（`*_env`）。启动时检查已启用组件所需的密钥，日志不打印密钥。

- **Mac**：密钥存 macOS 钥匙串，运行前注入环境变量（模板见 `.env.example`）。MinerU Token 已存放在钥匙串 `mineru.api.token` 条目，账号是当前系统用户。
- **VPS**：部署时存入权限为 600 的环境文件，不进 git。
- **Token 会过期**：此前一个旧 Token 已过期并返回 401，而当前 Token 不是 JWT，无法从本地读出到期时间。适配器收到认证失败时必须抛出 `AuthFailed` 并告警，不计入重试。

## 13. 防止代码腐化的机制

规则要靠工具执行，不靠自觉：

| 风险 | 机制 |
|---|---|
| 依赖方向被破坏 | `import-linter` |
| 流程代码出现具体来源名 | `tests/architecture/`，`application`、`ports` 中出现就失败 |
| 适配器行为不一致 | `tests/contracts/` 统一契约测试，新适配器必须通过 |
| 外部 API 变化不被发现 | 录制响应做回放测试；线上 `SchemaChanged` 告警 |
| 配置拼写错误或默默失效 | Pydantic `extra="forbid"`，`litfactory doctor` 启动自检 |
| 数据库结构漂移 | Alembic 迁移，禁止手动改表 |
| 数据被意外修改 | 领域模型不可变（frozen），`feedback` 等记录只追加 |
| 文件和函数膨胀 | 文件不超过 400 行，函数不超过 50 行（`tests/architecture/` 检查） |
| 决策失传 | 重要决策写 `docs/adr/` |
| 回归 | 单元、契约、集成、E2E 四层测试，覆盖率不低于 80% |

**故意不做**（以及什么时候才做）：

| 不做 | 原因 | 触发条件 |
|---|---|---|
| 微服务、消息队列（Redis 等） | 单人单机，SQLite 队列足够 | 需要多机并发处理 |
| PostgreSQL | SQLite 足够，SQLAlchemy 留了迁移空间 | 出现多个并发写入方 |
| 向量数据库 | 目前没有语义检索需求 | 要做“问文库” |
| 动态插件加载 | 显式注册表更容易调试 | 需要第三方安装插件 |
| 网页后端 | 静态站点已能满足展示 | 要做登录或评论 |
| 支持多种数据库 | 目前只有 SQLite。`ports/store.py` 的接口只用来维持依赖方向 | 出现第二种实现 |
| 给切分器抽象接口 | 目前只有一种实现 | 出现第二种实现 |

## 14. 用变更场景检验架构

| 场景 | 需要改动的文件 | 不用动的部分 |
|---|---|---|
| 新增 Scopus 信号源 | 1 个适配器、`bootstrap.py` 注册表、`sources.yaml`、录制数据 | 流程、评分、发布 |
| 下线某信号源 | `sources.yaml` 一行 | 其他全部 |
| 调权重 | rubric JSON，执行 `rescore` | 代码；不产生 LLM 费用 |
| 新增评价维度 | rubric JSON，执行 `reassess` | 代码 |
| 切换研究主题 | 新建主题 YAML，执行 `activate` 和 `backfill` | 已有 PDF 和解析结果 |
| 换成本地 MinerU | 新增解析适配器，改配置 | 切分、提取、评估 |
| PDF 迁到 R2 | 新增 `BlobStore` 适配器，迁移数据，改配置 | 业务代码 |
| 上线网页 | 启用 `static_site` 和路由 | 流水线其余部分 |
| 新增邮件推送 | 新增 Publisher 和 Renderer、配置 | 选文和 Digest |
| 换大模型 | `llm.yaml`，必要时加适配器 | 流程；只影响新论文，旧结果要重做时显式 `reassess` |
| MinerU 输出格式变化 | 该解析适配器的转换函数 | 下游全部 |

前三个场景和切换主题、改维度定义、公开渠道过滤都已写成测试（`tests/e2e/test_change_scenarios.py`）。

## 15. 实施顺序

每一步都要能运行和验证，不做只有框架的空壳：

1. **行走骨架（全部用假适配器，已完成）**：领域模型、数据库与迁移、任务调度、配置校验、rubric 算分、切分器、CLI。验证：E2E 测试从假信号源走到假渠道；重复运行不重复处理；改权重后 `rescore` 结果正确。
2. **MinerU 探针（已完成）**：MinerU 云端适配器（续查不重复提交、错误码分类、额度用完推迟）和 `pdf_text` 降级解析器；用 4 种版式的真实论文对比，结构样本录制为测试数据。
3. **真实主干**：Crossref、IEEE、手动投喂、Unpaywall、LLM、Telegram。
4. **校准与定时**：校准集、`rubric eval`、定时任务、告警、备份、保留清理。
5. **防漏检**：邮件提醒、引文追踪、覆盖率审计。
6. **扩展**：WoS、出版商全文 API、静态网页。

## 16. 开工前要实测的未知项

| 未知项 | 现状 | 影响 | 解决方式 |
|---|---|---|---|
| MinerU 云端输出字段 | 已测 IEEE、NeurIPS、ACM、中文期刊各一篇；Elsevier、Springer、扫描件未测 | 归一化实现 | 接入真实信号源后按实际论文补充 |
| MinerU 配额 | API 文档为每天 1000 页、单文件 200 页，旧限制页为 2000 和 600 页；单次批量有 50 和 200 两种说法 | 每日上限 | 按 1000、200、50 配置 |
| MinerU 对评分的增益 | 已比较送入模型的内容：英文论文两者持平，中文期刊 MinerU 明显更好。对最终分数的影响还没测 | 是否值得为每篇深评论文消耗额度 | 接入大模型后用同一批论文比较分数 |
| Telegram 限制 | 消息 4096 字符、下载 20MB、`callback_data` 64 字节是记忆值，未核实 | 渲染和按钮设计 | 实现前查文档，写入契约测试 |
| IEEE API 配额 | 文档页未列出 | 调用频率 | 申请 Key 后确认 |
| WoS 返回字段 | 未确认是否含摘要 | 是否需要补全 | 实测 |
| R2 和 Pages 免费额度 | 仅来自第三方（R2 10GB、Pages 500 次构建/月） | 部署成本 | 部署前查官方价格 |
| 存储估算 | 假设值 | 保留策略参数 | 上线后用 `stats` 校正 |
| 公开网页是否人工审核 | 模型可能被论文内容诱导写出不当文字，去链接挡不住文字本身 | 公开渠道的风险 | 第 6 步上线网页前决定 |
