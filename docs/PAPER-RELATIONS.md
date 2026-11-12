# 首次入库与论文关系核实

首次添加、第一次打开或首条批注会为论文登记一次入库事件。后台只处理这篇新论文，从此前已入库且仍在文库的论文中选择候选，用 `relate` 核实关系并保存双方原文。

## 自动处理与历史保护

迁移 0019 为既有文库建立基线，不创建任务。退出文库再进入不会重复触发。首次事件、用户写操作和 `memory_admit:{paper_id}` 任务同事务保存，以入库递增序号区分先后，支持同秒及并发操作。

阅读页通过 `POST /api/papers/{paper_id}/open` 登记首次打开，不依赖滚动；重复打开保留既有阅读状态。读取论文、事实、候选、关系和历史的 GET 均不写入或调用模型。渠道加入外部文献库仍是独立操作。

`memory_admit` 只为新论文推进获取、解析、提取、ReferenceList 和已就绪本地向量。重试及本地前置等待沿用原调度；已终止、缺少原文、退出文库或配置变化会明确记录结束原因。旧对端不自动提取或重算。自动嵌入禁止下载模型，缺少资产时使用其他候选信号；显式 [`memory reembed --execute`](LOCAL-EMBEDDINGS.md) 仍可首次下载。

首次入库未配置 `relate` 时，本事件不核实关系，后来配置也不补算。新论文仍可按已有获取、解析及提取配置准备事实。历史核实通过下面的显式回填完成。

## 配置、预算与冻结输入

`config/memory.yaml` 的可选配置：

```yaml
relate:
  token_budget: 8000
  max_fragments_per_side: 4
  fragment_chars: 1600
```

`token_budget` 为 512–64000，片段数为 1–16，片段长度为 100–10000 字符。完整系统提示、清洗后的 JSON 和隔离包装均计入预算，整对放不下就省略，不发送半对。候选数量由 `candidates.max_pairs` 控制，默认 5，计分沿用 [候选策略](MEMORY-CANDIDATES.md)。

`relate` 是第八个模型任务：

```yaml
tasks:
  relate:
    provider: main
    model: YOUR_MODEL
    prompt: relate
    daily_budget_usd: 0.2
```

新建设置默认增加每日 0.2 美元任务限额，可改为其他非负值或显式清除；设为 0 可暂停。全局与任务预算在实际请求前检查，等待不增加失败次数，次日恢复。估费按当前价目表、实际输入及每对约 1200 个输出 token 计算，属于经验估计；未知价格返回 `null`。价格变化要求重新确认预览，但保留已有输入缓存。限额调整不会改变生产者指纹。

队列冻结完整候选窗口、实际消息、来源指纹、配置、语言、预算及费用 scope。执行前按同一窗口重建核对，新增论文不扩张旧任务。模型返回后重新读取实际发送的文档，核对所有已发片段的当前原页，再逐侧验证回答引用；同一地址重解析覆盖也会被检查。只修改未发送尾部且已发片段仍匹配时可以继续。

只发送当前提取的 `problem`、`approach`、`method`、`claims` 真实引文及可定位引用句；用户笔记不发送。失效提取、不可读文档和无法定位的片段在候选排序前排除，全库身份别名保留，避免消除歧义。

## 结果、缓存与反馈

关系类型：

| 类型 | 方向 |
| --- | --- |
| `extends` | A 扩展 B |
| `uses_method_of` | A 使用 B 的方法 |
| `compares_with` | A 比较 B |
| `contradicts`、`similar_approach`、`same_problem` | 对称关系，双端按论文 ID 排序，原文随之交换 |

每条关系必须对应实际发送的一对，双方分别核对论文 ID、文档、零基页码、真实原页和已发连续片段。另一侧同页、元数据、页标记、未发尾部、跨片段拼接和冲突重复不能成为证据。引用失败整条丢弃。

批次按论文、完整输入和生产者精确缓存。无候选、无关、空列表或全部核验失败均保留完成凭据，不重复收费。新有效模型判断可替代旧模型判断；无关和核验失败不能推翻旧有效判断。关系内容和证据冻结，状态为 `valid`、`superseded` 或 `rejected_by_user`，历史不删除。

用户可确认、删除或改类型。每次动作带唯一 `request_id`，相同请求重放不会撤销后来的决定；换目标或动作复用 ID 会被拒绝。改类型追加用户来源判断并保留原证据。用户决定的双端从后续自动核实和回填中跳过，旧删除可由明确确认恢复。关系反馈不改变推荐卡片的保存、忽略及待保存队列。

已退出文库的关系可以保留历史确认和删除；新增改类型需要双方仍在文库，否则返回 422，零写入。

## 显式回填

```bash
uv run litgleaner memory backfill
uv run litgleaner memory backfill --execute --plan-hash <preview.plan_hash>
uv run litgleaner run --stage relate
```

默认预览完整文库 ID、每篇候选与遗漏、对数、精确缓存和预计费用。按论文 ID 稳定排序，每篇只与此前前缀比较，每一无序对最多发送一次；每篇仍受 N 和输入预算限制。未选中不代表已核实无关，详情保留在各论文计划和警告中。

确认重建计划并核对指纹，CLI 持共享运行锁，仅排队，由常驻服务或 `run --stage relate` 消费。整次预览与确认分别共享文档缓存，同一归档最多读取一次，失败也缓存；归档读取不占确认写锁，最终短事务再次核对文库、事实、向量、反馈和配置。GET、预览和确认请求均不执行模型。

## HTTP 接口

| 操作 | 接口 |
| --- | --- |
| 首次打开 | `POST /api/papers/{id}/open` |
| 预览核实 | `POST /api/papers/{id}/relations/preview` |
| 确认排队 | `POST /api/papers/{id}/relations`，`{"plan_hash":"..."}` |
| 关系及核验状态 | `GET /api/papers/{id}/relations` |
| 任务及本 scope 费用 | `GET /api/papers/{id}/relations/jobs/{job_key}` |
| 批次历史、指定结果 | `GET /api/papers/{id}/relations/history`、`.../results/{batch_id}` |
| 反馈 | `POST /api/relations/{id}/feedback`，`{"request_id":"...","action":"confirm"}`；改类型另带 `kind` |
| 全库回填预览、确认 | `POST /api/memory/backfill/preview`、`POST /api/memory/backfill` |

接口沿用本地会话、Host、Origin 和 CSRF 校验；所有 POST 需要 CSRF。缺少核实模型时仍可读取及预览，确认返回 `model_configuration_required`。

统一 [`stale`](STALE.md) 新增 `relation` 与 `relate_batch`，全历史使用固定 17 次 SELECT 的一致快照，事务外核验，不逐条查库。有效、过期和无法完整核验与关系业务状态分别返回。当前用户决定不因模型配置变化失效；未被用户决定的旧模型历史仍独立核验，原文及提取变化仍提示。文库成员来自完整范围，向量缺席不影响关系状态。

离线验收覆盖真实临时数据库、队列、网关、API/CLI、双侧原文、费用和反馈竞争。真实模型关系质量、真实论文案例调参、可选嵌入 SDK 与 Windows 真机仍待实测。
