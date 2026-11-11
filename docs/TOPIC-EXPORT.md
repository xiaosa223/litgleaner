# 课题对比表与引用导出

从当前课题画像下已通过初筛的论文生成对比表和正式引用。全部读取本地数据库，不联网、读 PDF、调用模型或创建任务；暂停的课题、缺少模型配置和预算为零时仍可导出。

## 命令

```bash
uv run litgleaner export table --topic example-topic --format md
uv run litgleaner export table --topic example-topic --format csv --tier must_read,digest
uv run litgleaner export refs --topic example-topic --format bibtex
uv run litgleaner export refs --topic example-topic --format csl-json --only-read
```

标准输出只有选定格式的文件内容，可自行重定向保存。引用命令把需要人工核对的论文编号、标题及格式警告单独写到标准错误，避免混入 BibTeX 或 CSL-JSON。`--only-read` 明确表示只包含阅读状态为 `read` 的论文。

架构示例中的 `--read-only` 尚未定义语义，本实现不把它默认为已读筛选；CLI 暂不接受这个选项。GB/T 7714 按 ADR 0009 是可选适配器，当前未配置，不接受 `gbt7714` 格式；不把其他格式伪装成合规的国标引用。

## API

以下均为需要本机会话的 GET，不需要 CSRF，也不接受服务器文件路径：

| 路径 | 结果 |
|---|---|
| `/api/topics/{topic_id}/comparison-table?tier=must_read,digest` | JSON 对比行、课题指纹、筛选前总数和选定等级 |
| `/api/topics/{topic_id}/exports/table?format=md` | Markdown 下载；`format=csv` 返回 CSV |
| `/api/topics/{topic_id}/references` | 引用内容、格式警告和 `needs_verification` 原始元数据列表 |
| `/api/topics/{topic_id}/exports/refs?format=bibtex` | 纯 BibTeX 下载；`format=csl-json` 返回 CSL-JSON |

两个引用接口支持 `only_read=true`。下载使用固定 ASCII 文件名、`Cache-Control: no-store` 和 `X-Content-Type-Options: nosniff`；纯引用下载的 `X-Litgleaner-Needs-Verification` 表示被单列核对的条目数，具体名单从对应课题的 `/references` 读取。JSON 视图也可作为带核对名单的完整导出保存。未知课题返回 404，无效等级或格式返回 422。

## 范围与来源

- 范围由当前 `topic_id + topic_hash + relevant=true` 决定，不受首页推荐数量、已推送状态或文库成员资格限制。旧画像下的相关判断不混入当前范围；不会为导出重新初筛。
- 每次导出固定六次限定查询，在一个短事务中取得论文、别名、最新提取、最新匹配评估的当前策略评分、全文状态和阅读状态。读取次数不随论文数增长，不生成超长参数列表，渲染在事务外执行。
- 方法、数据集、指标结果和代码/数据状态来自 `extraction`；价值分来自规则计算的 `score`，目标理由来自 `assessment.goal_links`。三类来源分别标明，不复制判断为作者事实。
- 当前评分必须匹配画像、评价维度、权重策略以及文档/摘要、阅读语言和发送预算。最新评估没有当前策略评分时不回退旧评估。没有有效分数显示未评分，不变成零，也不参与 `--tier` 筛选。
- 提取输入与生产者指纹按 [EXTRACTION](EXTRACTION.md) 检查。旧结果仍可导出并标记过期；旧结果缺字段显示“未提取”，新结果显式空值显示“本次提取未获得可核验证据”。这不表示作者没有相应内容。
- 模型概括、作者引文和评估推荐理由分别标注，页码在人类输出中从 1 开始。核验标记必须是真正布尔 `true`；未核验内容保留明确标签。链接还需 `url_verified=true` 且地址词法合法，不能从未公开链接推断作者未公开代码。
- Markdown 对 HTML、表格分隔符、换行和链接语法转义；CSV 使用标准字段转义，并防止标题或编号被表格软件解释成公式。
- 正式引用复用 [个人导出](PERSONAL-EXPORT.md) 的来源元数据渲染器，只使用有效且无歧义的标识符。缺失或冲突标识符单列核对；不联网补全、不猜作者姓/名或书目信息。

课题页查看和下载按钮尚未接线。综述骨架需要单独的按需模型任务及结果契约，不由这些只读请求触发。
