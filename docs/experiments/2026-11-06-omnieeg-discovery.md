# 发现层对比：拾穗与 agent 直接调研（OmniEEG 初赛）

- 日期：2026-11-06
- 复现：`export LITGLEANER_CONFIG=$PWD/data/exp-omnieeg/config && uv run litgleaner run --stage discover`，然后运行 `data/exp-omnieeg/compare.py`（不进 git）
- 对照组：`研一/比赛/OmniEEG Challenge 2026/` 下 agent 于 10 月 24 日至 11 月 4 日完成的调研文本、方案与 `proposal/references.bib`（114 条）
- 原始数据：`data/exp-omnieeg/`（数据库、`bib_vs_factory.csv`、`factory_only_shortlist.csv`）

## 结论

1. **只比发现层。** 大模型目前是假适配器，初筛会让所有论文通过，评分没有意义；本次只运行了 `discover` 阶段。
2. **召回接近 agent。** agent 引用里与 EEG 基础模型直接相关的 53 篇，工厂找到 48 篇（91%）。另外 61 条是通用机器学习、分赛道经典方法和数据集，不在主题检索范围内，一篇也没找到。
3. **工厂找到 agent 完全没提到的相关工作。** 例如：NeuroLM（ICLR 2025）、Neuro-GPT、FEMBA、EEG-FM 综述（arXiv:2507.11783，已发表于 J. Neural Eng.）；直接对应方案主题“通道配置失配”的 HEAR、DIVER-0、LuMamba、Channel-Aware SSL；直接对应“线性探测 / LoRA”的 Open EEG Bench、基于 LaBraM 的 LoRA 运动想象研究（IEEE BCI 2026）。agent 撰写的文本中均检索不到这些名称。
4. **代价是噪声。** 共 1075 篇候选，其中工厂独有的 1027 篇里，标题同时涉及 EEG 与基础模型或预训练的约 325 篇，其余多为情绪识别的自监督方法、fMRI 等。没有真实的初筛就无法直接使用。

**建议**：工厂适合做调研的“候选池与查漏”，agent 适合做筛选、精读和论证。接入真实大模型做初筛之前，工厂的产出只能当待筛清单。

## 设置

| 项 | 值 |
|---|---|
| 主题 | 只依据参赛手册编写：5 组关键词短语 + 手册点名的 LaBraM、CodeBrain 作为引文追踪种子 |
| 信号源 | OpenAlex、arXiv、Semantic Scholar、OpenReview、Europe PMC，`lookback_days: 2000`（约 2021-05 起） |
| 耗时与费用 | 3 轮共约 2.5 分钟，没有调用大模型 |

| 信号源 | 关键词检索 | 引文追踪 |
|---|---|---|
| Semantic Scholar | 341 | 554 |
| OpenAlex | 367 | 99 |
| arXiv | 230 | — |
| OpenReview | 207 | — |
| Europe PMC | 157 | — |

引文追踪方面，Semantic Scholar 找到的数量是 OpenAlex 的 5 倍以上。

## 漏检的 5 篇及原因

| 论文 | 原因 |
|---|---|
| BIOT、BENDR、S-JEPA、通道无关对比预训练评估（arXiv:2410.19842） | 早于种子论文，不会引用 LaBraM；标题和摘要又不含主题短语。引文追踪只查“谁引用了种子”，不查“种子引用了谁” |
| OSF（睡眠基础模型） | 关键词没有覆盖“sleep foundation model” |

## 暴露的工厂问题

| 问题 | 影响 | 可选改进（未实施） |
|---|---|---|
| 引文追踪只能向后查引用者，不能向前查参考文献 | 一次性回溯调研会漏掉奠基工作 | 增加“种子参考文献”查询，或把奠基论文也列为种子 |
| 某一信号源限频时，本轮剩余的发现任务全部推迟 | OpenReview 返回 429 后，Semantic Scholar 没有执行，需要手动再运行一轮 | 限频只推迟同一来源的任务 |
| OpenReview 匿名投稿没有作者，无法与 arXiv 版本合并 | 27 组同名论文重复入库 | 作者缺失时允许完全同名的论文进入待确认 |
| OpenReview 每个词最多翻 3 页 | 排在后面的结果没有检查 | 一次性回溯时调大上限 |

## 公平性说明

- agent 引用的是它选择保留的文献，不等于它见过的全部文献；因此补充检索了 agent 撰写的三份调研文本、方案章节和审稿记录，上述“完全没提到”的结论以这些文本为准。
- “与 EEG 基础模型直接相关的 53 篇”由人工划定，见 `data/exp-omnieeg/scope.py`。
- 工厂在 11 月 6 日运行，比 agent 晚两天；发表于 11 月 4 日之后的工厂独有论文只有 3 篇，不影响结论。
- 关键词由熟悉该领域的人编写，比只读手册的新手写得更好，召回可能偏乐观。
