# 个性化导读、回答卡片与复习

实现第 12.7 节（[ADR 0007](adr/0007-knowledge-base-and-personal-guidance.md)）的闭环：读前导读 → 读后用自己的话回答 → 评价 → 掌握程度沉淀进知识库 → 按间隔复习 → 下一篇导读按已掌握的内容定制。实测见 [2026-11-14](experiments/2026-11-14-guidance-real-model.md)。

## 用户看到什么

| 位置 | 内容 |
|---|---|
| 阅读页“导读” | “生成我的导读”（附费用估计）；生成后：这篇在做什么、带着读的三个问题、先弄懂的概念（按掌握程度标“新概念 / 见过 / 你已掌握”，新概念给类比并写明失效之处）、先读哪里。知识库变化后提示“可以重新生成”，不自动重做 |
| 阅读页“读后” | 三张回答卡片，先写后看：“提交，看评价”或“先存着”；评价逐个概念给“对 / 漏了 / 理解偏了”，附核验过的原文、参考答案和下一步建议；“评价有误？”写入反馈；修改回答保留旧版本 |
| 知识库 | 概念列表按掌握程度筛选；详情里可以直接改等级，显示论文定义和自己写过的回答（理解偏了的标明） |
| 知识库概念详情、页顶 | 相关概念（先懂 / 是它的前置 / 相似 / 对比，附理由；模型提议的可确认或删除，点名字跳转）；疑似同一概念“合并 / 不是”，合并后按一个概念记录 |
| 知识库“笔记” | 全部论文的笔记和高亮，最近修改的在前，可筛选，点出处跳回原页 |
| 设置“数据”、知识库“导出 Markdown” | 个人导出 zip：每篇论文、每个概念一个 Markdown，含回答的全部修订、模型评价、掌握程度和下次复习时间；不调用模型 |
| 今天、知识库“复习” | 到期概念：先回忆，再看当时写的，自评记得 / 模糊 / 忘了，不调用模型 |

## 规则

- 掌握程度（`domain/mastery.py`，纯函数，带 `policy_hash`）：读过含该概念的其他论文为“见过”；回答评价中该概念判“对”到“能解释”；对比、迁移题整题判对才到“能应用”；在“能解释”上复习累计 3 次“记得”也到“能应用”。评价只升不降；用户改等级优先，之后的评价和复习可在此基础上提升。
- 复习间隔 1、3、7、14、30 天：记得进一档，模糊停在原档，忘了回到第一档（不降级）。
- 掌握事件只存事实（评价的题型和判断、复习自评、用户定的等级），等级在读取时算出；规则变了零费用重算。
- 导读输入只含这篇论文的关键概念（最多 8 个）及其掌握程度、读者整题判对的旧回答原话、最多 20 个已掌握概念的名称和学到它的论文标题（供判断是否同一领域；只有机制真正相近时才用知识库类比和对比，跨领域时用日常类比），以及研究画像。指纹覆盖全部输入。
- 没有提炼结果时不生成导读（概念来自提炼）。
- 模型任务 `guide`、`evaluate`（`config/llm.yaml`），提示词在 `prompts/`；输出过 JSON Schema，原文引用逐条 `verify_quote`，没通过的不显示，整段标“模型推断”。

## 接口

| 方法 | 路径 | 说明 |
|---|---|---|
| GET | `/api/papers/{id}/guide` | 导读、是否过期、生成计划（`plan_hash`、费用估计）、三张卡片 |
| POST | `/api/papers/{id}/guide` | `{plan_hash}`，确认后排队 |
| POST | `/api/papers/{id}/guide/{guide_id}/answers` | `{question_index, text, evaluate}`，追加一次修订 |
| POST | `/api/papers/{id}/answers/{card_id}/evaluate` | 对已存回答请求评价 |
| POST | `/api/papers/{id}/answers/{card_id}/wrong` | 标记评价有误 |
| GET | `/api/knowledge/cards` | 概念、掌握程度、是否到期、回答 |
| POST | `/api/knowledge/concepts/{id}/level` | `{level}` 用户改等级 |
| GET | `/api/knowledge/review` | 到期概念 |
| POST | `/api/knowledge/concepts/{id}/review` | `{rating}` 自评 |
| GET | `/api/knowledge/merges` | 待确认的疑似同一概念 |
| POST | `/api/knowledge/decisions` | `{target, decision}` 确认或删除关系、合并提议 |
| GET | `/api/notes` | 全部笔记和高亮 |
| POST | `/api/exports/mine` | 个人导出 zip（`{include_pdfs}`） |

表：`explanation`（`kind=guide`）、`answer_card`、`answer_evaluation`、`mastery_event`（迁移 0022），`paper_correction`（0023），`concept_link`、`concept_merge_candidate`、`concept_decision`（0024），全部只追加。
