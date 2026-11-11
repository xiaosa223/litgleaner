# 信号源与全文来源

所有真实来源默认关闭，在 `config/sources.yaml` 和 `config/app.yaml` 里改成 `enabled: true` 后生效。
启用前先设置联系邮箱，所有来源共用：

```bash
export LITGLEANER_CONTACT_EMAIL="you@example.org"
litgleaner doctor                                # 检查必需和可选的环境变量
uv run python scripts/probe_sources.py           # 真实网络联调，不写数据库
```

## 信号源

| 名称 | 覆盖 | 期刊 | 关键词 | 引文 | 作者 | 收件箱 | 环境变量 |
|---|---|:-:|:-:|:-:|:-:|:-:|---|
| `crossref` | 所有登记 DOI 的出版物 | ✓ | ✓ | | | | — |
| `openalex` | 全学科，约 2.5 亿篇 | ✓ | ✓ | ✓ | ✓ | | `OPENALEX_API_KEY`（可选） |
| `semantic_scholar` | 全学科，偏计算机和生物医学 | | ✓ | ✓ | ✓ | | `SEMANTIC_SCHOLAR_API_KEY`（可选） |
| `core` | 全球机构知识库的开放论文 | | ✓ | | | | `CORE_API_KEY`（必需，免费注册） |
| `arxiv` | 物理、数学、计算机等预印本 | | ✓ | | ✓ | | — |
| `openreview` | 机器学习会议投稿 | | ✓ | | | | — |
| `pubmed` | 生物医学 | ✓ | ✓ | | ✓ | | `NCBI_API_KEY`（可选） |
| `europe_pmc` | PubMed 加 bioRxiv、medRxiv 等预印本 | ✓ | ✓ | | ✓ | | — |
| `zotero` | 你自己的 Zotero 文献库 | | | | | ✓ | `ZOTERO_API_KEY`（仅 web 模式） |

各列对应主题文件里的配置：期刊 = `journal_sets`，关键词 = `query`，引文 = `seeds.dois`，作者 = `seeds.authors`。收件箱每次运行都会查。

OpenAlex 和 Semantic Scholar 另外支持 `ReferenceList`：查论文引用的参考文献，按单次条目上限取历史结果，不使用日常日期窗口或游标；原条目、引用句和来源标记保存为引文事实。显式刷新与本地归档登记接口见 [CITATIONS.md](CITATIONS.md)。它尚未并入课题调研流程。

### 通用选项

每个信号源都支持下面这些选项（写在 `options` 下）：

| 选项 | 默认值 | 说明 |
|---|---|---|
| `lookback_days` | 7 | 第一次运行回看多少天 |
| `overlap_days` | 3 | 之后每次和上次窗口重叠多少天，用来接住晚入库的记录（OpenAlex 默认 60） |
| `max_records` | 500 | 单次最多取多少条。超过时结果被截断、游标不前进，下次重新抓取同一窗口 |
| `min_interval_seconds` | 各来源不同 | 两次请求的最小间隔 |
| `timeout_seconds` | 30 | 单次请求超时 |

游标是上次抓取的日期（Zotero 例外，见下）。重叠窗口带来的重复记录由去重阶段合并。

## 全文来源

按 `config/app.yaml` 里 `fulltext` 的顺序尝试，第一个拿到 PDF 的生效。单个来源出错不影响后面的来源；全部没拿到且有来源出错时，任务稍后重试。

| 名称 | 依据 | 记为 | 环境变量 |
|---|---|---|---|
| `zotero` | 你 Zotero 里已有的 PDF | `user_supplied` | `ZOTERO_API_KEY`（仅 web 模式） |
| `arxiv` | arXiv 编号，或 `10.48550/arXiv.*` 形式的 DOI | `open` | — |
| `europe_pmc` | PubMed Central 开放获取子集 | `open` | — |
| `unpaywall` | DOI 的开放获取 PDF | `open` | `LITGLEANER_CONTACT_EMAIL`（必需） |
| `openalex` | OpenAlex 记录的开放获取位置 | `open` | `OPENALEX_API_KEY`（可选） |
| `semantic_scholar` | `openAccessPdf` | `open` | `SEMANTIC_SCHOLAR_API_KEY`（可选） |
| `core` | CORE 收录的全文 | `open` | `CORE_API_KEY`（必需） |
| `crossref_tdm` | 出版商在 Crossref 登记的文本挖掘链接 | 知识共享许可为 `open`，否则 `subscribed` | — |

`subscribed` 和 `user_supplied` 的文件不会出现在公开渠道。

**机构订阅**：`crossref_tdm` 不带任何出版商凭据。只有在校园网或学校 VPN 里运行时，出版商才会对订阅文章放行；被拒时返回“没有全文”，不会绕过访问控制。

**下载安全**：PDF 链接来自第三方数据，下载器的限制如下。
- 只访问 http(s)，每一跳重定向都检查目标不是内网或本机。
- 单个文件不超过 80 MB，单个链接总耗时不超过 180 秒。
- 内容以 `%PDF-` 开头才接受。
- 不访问内网主机名（`localhost`、`.local`、`.lan`、`.internal`、不带点的主机名等），不允许从 https 重定向到 http，不带联系邮箱，带密钥的请求头不随重定向发往其他主机。
- 使用 Clash、Surge、Shadowrocket 等代理的 fake-IP 模式时，域名会被解析到 `198.18.0.0/15`。下载器默认放行这种解析结果，否则无法下载；URL 里直接写的 IP 不享受这个例外。对安全要求更高的部署，在全文来源的 `options` 里写 `allow_proxy_fake_ip: false` 关闭。

## Zotero

| 选项 | 说明 |
|---|---|
| `mode` | `local`（默认）读本机数据库；`web` 读 Zotero Web API，适合部署在服务器上 |
| `data_dir` | 本机数据目录。不写时按顺序查找：Zotero 配置里的 `dataDir` → `~/Zotero` |
| `collection` | 只读某个分组（名称或 key） |
| `lookback_days` | 第一次运行导入最近多少天加入的条目，默认 30；设为 `null` 导入全部历史 |
| `user_id` / `group_id` | web 模式必填其一 |

- local 模式把数据库复制到临时目录后只读打开，读完删除副本，不会写你的数据库；Zotero 开着也能读。
- local 的游标是已处理条目的最大加入时间，web 的游标是文库版本号。
- 这里只读。把推送的论文写进 Zotero 是另一条路径（本地 API 写入），见 [CHANNELS.md](CHANNELS.md)。

## 已知限制

| 来源 | 限制 |
|---|---|
| 所有来源 | 单次达到 `max_records` 时游标不前进，下次重新抓取同一窗口，不会丢数据；但如果每天的量都超过上限，会一直截断，日志每次都有告警，需要收窄查询或调大上限 |
| `openalex` | 按入库日期筛选需要付费套餐，只能按出版日期筛选，所以默认重叠 60 天。匿名的关键词检索在服务端繁忙时经常被限流，建议配置 key |
| `openalex`、`semantic_scholar` | 作者追踪只在首个检索结果明确是这个人时才采用；同名档案多的作者会告警并跳过 |
| `semantic_scholar` | 无 key 时经常返回 429，任务会按限流推迟 |
| `core` | 后端不支持按入库日期筛选，只能按出版日期；晚收录的旧论文抓不到 |
| `openreview` | 检索只按相关度排序、不能按日期过滤，新投稿可能排在很后面查不到 |
| `arxiv` | 要求请求间隔至少 3 秒，单次请求常需十几秒 |
| `europe_pmc` | europepmc.org 的 PDF 地址在人机验证后面，全文改从 PMC 开放子集的公开存储和出版商开放链接获取 |
| `zotero` | local 模式下，从其他设备同步过来、加入时间早于游标的条目会漏掉；同一秒批量导入超过 `max_records` 条时，超出的部分会漏掉；每次取全文都会复制一次数据库，库很大时较慢 |
| `crossref_tdm` | 知识共享许可都记为 `open`，包括 NC、ND 等限制性条款 |

不接入的来源：DBLP 的 API 启用了人机验证，自动化访问不可行；Google Scholar 没有 API。这两类可以用邮件提醒的方式接入（计划中）。
