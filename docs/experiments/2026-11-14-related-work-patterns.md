# 2026-11-14 真实论文怎样写“前人工作 + 不足”

目的：在设计“调研时为每篇论文生成一句可用于综述的话”之前，先看真实论文的相关工作和综述怎么写，避免生成千篇一律的“X 提出……，效果……，但……”。

## 样本

从 arXiv 取全文（PDF 用 `pdftotext`，较大的综述用 arXiv HTML 转文本），只读相关工作、引言里讨论前人的段落和综述正文。

| arXiv | 论文 | 类型 | 读的部分 |
|---|---|---|---|
| 1901.05498 | Roy 等，Deep learning-based EEG analysis: a systematic review（2019） | 综述，大模型普及前 | 数据增强、跨被试、训练流程几节 |
| 2604.27033 | Cross-Subject Generalization for EEG Decoding: A Survey（2026） | 综述 | 第 3 节方法分类 |
| 2508.15716 | Foundation Models for Cross-Domain EEG Analysis: A Survey（2025） | 综述 | 意图识别、EEG 到文本两节 |
| 2305.10351 | BIOT（NeurIPS 2023） | 研究论文 | 引言 |
| 2405.18765 | LaBraM（ICLR 2024） | 研究论文 | 附录相关工作 |
| 2412.07236 | CBraMod（ICLR 2025） | 研究论文 | 附录相关工作 |
| 2503.11651 | VGGT（CVPR 2025） | 研究论文，计算机视觉 | 第 2 节相关工作 |

另取了 2101.01169（Vision Transformer 综述），未细读。样本以英文、机器学习方向为主；2025 年以后的两篇综述可能有 AI 辅助写作。

## 观察

### 1. 段落按问题或方法分组，论文是组里的成员

7 篇全部如此。每段先用一句话定义这一类方法的共同机制，再逐篇列出。例如 2604.27033 在“分布对齐”一节先解释 MMD 和 CORAL，再写：

> Hang et al. [32] propose … DDAN, which integrates MMD into a deep convolutional network … Similarly, Wei et al. [85] employ MMD within a federated "Sandwich" framework …

### 2. 每篇论文通常只占一个分句，写的是“它和同组其他论文的区别”

常见结构是“作者/方法名 + 动词 + 区别于邻居的机制”，并用关系词串起来：`Similarly`、`extend this with`、`an improved version of`、`adopts an alternate strategy`、`Instead`。

> Brant-2 (Yuan et al., 2024) represents an improved version of Brant, tailored for the integration of scalp EEG and intracranial EEG. Brant-X … extends this framework to achieve alignment between different physiological signals and EEG.（CBraMod）

同一篇论文在不同组里的描述会不同，它强调的是与该组相关的那一面。

### 3. 单篇论文的句子里几乎不同时出现“效果”和“局限”

- 研究论文的相关工作基本不写效果。只有综述 1901.05498 写了具体数字，而且数字来自原文实验（如 “from 49.6% … to 74.3%”）。
- “X 提出……取得……但……”这种单句结构，在读过的段落里**一次都没有出现**（只读了上表所列部分，不是全文逐句统计）。

### 4. 不足通常放在一组末尾，集体陈述，并且正好指向本文要解决的问题

> However, most of existing EEG foundational model [model] the spatial and temporal dependencies between all EEG patches together, ignoring the unique structural characteristics of EEG signals.（CBraMod，之后提出 criss-cross 结构）

> However, all existing studies either concentrate on specific BCI tasks or only employ small-size datasets and models …（LaBraM，之后用大规模数据预训练）

> These models (…6 篇引用…) predominantly focus on biosignal samples with fixed formats for specific tasks …（BIOT，之后处理不同通道和长度）

> Some concurrent works … explore replacing DUSt3R's test-time optimization with neural networks, though these attempts achieve only suboptimal or comparable performance to DUSt3R. Instead, VGGT outperforms …（VGGT）

研究论文用这种“共同不足”来引出自己的工作。用户说的“衬托我们的研究”，在真实论文里是**按组**完成的，不是逐篇完成的。

### 5. 单篇的批评要有具体依据

样本里少数针对单篇的批评都有出处：

- 指出实验设计的问题（1901.05498：“Despite impressive results, … they also compared LeNet and ResNet to an SVM which had an accuracy of 74.2% …”）；
- 引用其他论文的质疑（2508.15716：“Jo et al. [67] argued that the generative power of large language models may produce coherent outputs even when given random … EEG inputs”）。

没有出现凭空断言“该方法泛化性不足”的写法。

## 对设计的含义（提议，未定）

1. 每篇论文的句子素材，应是“机制分句”，即它做了什么、和同组相比有什么不同，而不是“方法 + 效果 + 局限”三段式。
2. “不足”应在**组**这一级生成，并与研究画像的目标挂钩：这组方法共同没有解决我的哪个问题，附组内论文编号。这和第 14 节综述骨架的分组天然契合。
3. 效果只在有原文数字和页码时给出；单篇局限只用作者自述，或另一篇论文的引用句（`citation` 已保存引用句和意图）作依据，模型推断要另行标明。
4. 关系词（类似、扩展、改进、相反）可以由已有的论文关系（ADR 0010、`relate`）和引用关系提供，不需要模型自由发挥。

## 未覆盖

- 中文学位论文“国内外研究现状”的写法未看。它可能更常见逐篇的“某某等人提出……，但……”，需要另取样本。
- 只看了机器学习方向；医学、社会科学的综述写法可能不同。
