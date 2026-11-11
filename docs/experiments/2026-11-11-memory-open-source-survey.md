# 论文间记忆：开源实现调研（2026-11-11）

问题（用户 2026-11-11）：一篇论文入库后，以后读到一篇方法类似、或承上启下的新论文时，带读的模型会不会提一句，还是入库了就不管了。

现状：ADR 0007 的知识库以**概念**为单位（概念、别名、出现位置、回答卡片、掌握程度）。**论文之间的关系**没有存储，导读输入里的“用户读过的相关论文”也没说怎么找。所以按当前设计，入库后基本就不管了。

调研方法：读源码和论文原文，许可证与活跃度来自 GitHub API（2026-11-11）。【源码】是读过源码，【论文】是读过论文，【推测】是判断。浅克隆在会话临时目录，没有进仓库。

## 逐项结论

| 项目 | 许可证 / 状态 | 核心机制 | 借 | 不借 |
|---|---|---|---|---|
| A-MEM（agiresearch/A-mem） | MIT；2025-12 后无推送 | 每条笔记由模型生成关键词、标签；向量取 5 个近邻，模型决定加链接或改写邻居（`memory_system.py` `process_memory`）【源码】 | 笔记带结构化元数据；链接存为 id 列表 | 改写别的记忆（违反事实只追加）；每条都调模型；向量库 |
| Mem0（mem0ai/mem0） | Apache-2.0；活跃 | v3 已放弃 ADD/UPDATE/DELETE/NOOP，改为只追加、去重靠哈希，冲突交给检索排序；实体用 spaCy 规则抽取；检索分 = 语义 + BM25（sigmoid 归一化）+ 实体加成（`mem0/utils/scoring.py`）【源码】 | 只追加；BM25 与实体加成的混合打分（可用 FTS5 实现） | 主路径依赖向量库 |
| Letta / MemGPT | Apache-2.0；已迁到 letta-code | 三层：常驻的 core block、对话 recall、archival 段落；agent 自己改写记忆【源码】 | 分层：研究画像常驻提示词，其余按需检索 | agent 自编辑记忆，不可审计、无指纹 |
| Zep / Graphiti | Apache-2.0；v0.31.2 | 关系边带有效期；被新证据推翻的边只写 `invalid_at`，不删除；每条边记录来源片段；实体名先用 MinHash 去重再交模型【源码】 | 判断失效不删除、记录被谁取代；关系带来源；同一对节点只和自己的旧边比较 | 只支持图数据库；每段输入多次调模型 |
| LightRAG / GraphRAG | MIT | 模型抽取实体关系、社区摘要；GraphRAG 的 fast 模式用名词短语共现建边，只在社区报告时调模型【文档】 | 名词短语共现可以用 SQLite 表实现 | 社区摘要对几百篇的个人库价值低、费用高 |
| PaperQA2 | Apache-2.0 | 从高分论文出发沿引用前后各走一层，只保留被至少 1/3 起点论文共同引用的文献（arXiv 2409.13740 §8.1.1）；开源版不含这个工具【论文】 | 共同引用阈值；“高分的才作为起点” | 每个文本块都调一次模型做摘要打分 |
| CiteSee（CHI 2023） | 未找到源码 | 读论文时，被引文献若在用户库里、读过或收藏过就标出，卡片里显示“你在哪篇论文的哪句引文处见过它”；“再次遇到的引用”这个信号显著有效，全局引用数是负向的【论文】 | 最便宜、证据最强的信号：当前论文引用了你读过的论文，加上引用句和你当时的笔记 | — |
| 引用意图（SciCite、ACL-ARC、MultiCite） | SciCite Apache-2.0，不再维护 | 背景、方法、结果比较；ACL-ARC 多出 Extends、Uses、CompareOrContrast【论文】 | 关系类别的定义 | 本地跑分类模型：Semantic Scholar 已给出意图 |
| Obsidian 未链接提及 | 文档 | 纯文本匹配“提到了但没链接”的位置【文档】 | 用 FTS5 在笔记和正文里匹配库中论文的短标题、方法名、概念别名 | — |
| Semantic Scholar API | 免 key 共享额度 | `/paper/{id}/references?fields=contexts,intents,isInfluential` 免 key 可用，返回引用句和意图；推荐接口可用；SPECTER 向量免 key 时遇到 429【实测】 | 引用关系、引用句、意图作为事实写入 | 依赖向量 |

## 共同点

1. 成熟系统都在往“事实只追加、冲突交给检索或标记失效”收敛（Mem0 v3、Graphiti），与本项目的事实与判断分离一致。
2. 发现关系靠便宜的信号（引用、共现、文本检索），模型只用来核实或打标签；全部交给模型的方案（A-MEM、LightRAG）费用不可控。
3. 对读论文的人最有用的提示，是“这篇引用了你读过的那篇，引用句是……，你当时写过……”（CiteSee）。

## 未验证

Mem0 v2 四动作的细节；LightRAG 的具体实现文件；免 key 能否稳定取得 SPECTER2 向量；下面设计的真实费用和召回率——要用真实论文案例实测（见 ADR 0010 的验收）。

## 来源

- https://github.com/agiresearch/A-mem
- https://github.com/mem0ai/mem0（`docs/migration/oss-v2-to-v3.mdx`、`mem0/utils/scoring.py`）
- https://github.com/letta-ai/letta/tree/archive 、https://github.com/letta-ai/letta-code
- https://github.com/getzep/graphiti
- https://github.com/HKUDS/LightRAG 、https://github.com/microsoft/graphrag/blob/main/docs/index/methods.md
- https://github.com/Future-House/paper-qa 、https://arxiv.org/abs/2409.13740
- https://arxiv.org/abs/2302.07302（CiteSee）
- https://github.com/allenai/scicite 、https://github.com/allenai/multicite
- https://github.com/obsidianmd/obsidian-help/blob/master/en/Plugins/Backlinks.md
- https://api.semanticscholar.org/api-docs/
