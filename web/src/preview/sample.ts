// 界面预览用的示例数据：课题调研和知识库的后端还没有，这里的内容只用来看界面，页面上会标明“示例数据”。
// 论文都是公开的经典论文；数量、分组、回答等数字和文字是编的。作者原话逐字取自样本论文
// （arXiv 1706.03762），页码已核对。后端接上后删除本目录（见 ROADMAP 的已知差异）。
import type { Concept, NoteEntry, ReadingRoute, Survey, TopicMap } from "../types";

const paper = (title: string, by: string, venue: string, year: number, cited: number, why: string) => ({ title, by, venue, year, cited, why });

export const SAMPLE_TOPIC = "注意力机制与 Transformer";

export const SAMPLE_SURVEY: Survey = {
  passed: 72,
  read: 4,
  rounds: [
    { round: 1, scope: "历史检索，加种子论文的参考文献和被引", candidates: 412, fresh: 412, passed: 63, cost_usd: 0.21 },
    { round: 2, scope: "第一轮高分论文的参考文献和被引", candidates: 288, fresh: 97, passed: 9, cost_usd: 0.05 },
  ],
  sources: [
    { name: "OpenAlex", found: 356 },
    { name: "Semantic Scholar", found: 291 },
    { name: "arXiv", found: 118 },
  ],
  gaps: ["没有开放接口的中文数据库（知网、万方）", "检索式之外的语言"],
};

export const SAMPLE_ROUTE: ReadingRoute = [
  {
    key: "review", label: "先读综述", note: "先看全貌，知道这个方向分成哪几块",
    papers: [
      paper("A Survey of Transformers", "Lin 等", "AI Open", 2022, 11, "按结构改进、预训练、应用三条线梳理，对应目标 g1"),
      paper("Efficient Transformers: A Survey", "Tay 等", "ACM Computing Surveys", 2022, 8, "专讲降低注意力计算量的方法，对应目标 g2"),
    ],
  },
  {
    key: "foundation", label: "奠基论文", note: "被本课题里的论文引用最多",
    papers: [
      paper("Attention Is All You Need", "Vaswani 等", "NeurIPS", 2017, 61, "提出 Transformer，后面几乎每篇都以它为起点"),
      paper("Neural Machine Translation by Jointly Learning to Align and Translate", "Bahdanau 等", "ICLR", 2015, 34, "注意力机制最早用于翻译，理解“对齐”的来历"),
      paper("Sequence to Sequence Learning with Neural Networks", "Sutskever 等", "NeurIPS", 2014, 22, "编码器-解码器框架的出处"),
    ],
  },
  {
    key: "representative", label: "代表作", note: "阅读价值分最高",
    papers: [
      paper("BERT: Pre-training of Deep Bidirectional Transformers for Language Understanding", "Devlin 等", "NAACL", 2019, 29, "只用编码器做预训练，可借鉴其掩码任务设计"),
      paper("An Image is Worth 16x16 Words: Transformers for Image Recognition at Scale", "Dosovitskiy 等", "ICLR", 2021, 17, "把序列建模搬到图像，迁移到新模态的范例"),
    ],
  },
  {
    key: "recent", label: "最新进展", note: "近两年",
    papers: [],
  },
];

export const SAMPLE_MAP: TopicMap = {
  ungrouped: 5,
  groups: [
    { name: "基础结构", note: "注意力、位置编码、归一化等组成部件", count: 14, titles: ["Attention Is All You Need", "Layer Normalization", "Deep Residual Learning for Image Recognition"] },
    { name: "高效注意力", note: "降低长序列上的计算和内存开销", count: 19, titles: ["Reformer: The Efficient Transformer", "Longformer: The Long-Document Transformer", "Efficient Transformers: A Survey"] },
    { name: "预训练", note: "在大规模无标注数据上预训练再微调", count: 16, titles: ["BERT: Pre-training of Deep Bidirectional Transformers for Language Understanding"] },
    { name: "视觉与其他模态", note: "把同一结构用到图像等非文本数据", count: 18, titles: ["An Image is Worth 16x16 Words: Transformers for Image Recognition at Scale", "Swin Transformer: Hierarchical Vision Transformer using Shifted Windows"] },
  ],
};

const SOURCE = "Attention Is All You Need";

export const SAMPLE_CONCEPTS: Concept[] = [
  {
    id: "c1", name: "Scaled Dot-Product Attention", translation: "缩放点积注意力", level: 3, due: false, papers: 3,
    definition: { quote: "An attention function can be described as mapping a query and a set of key-value pairs to an output, where the query, keys, values, and output are all vectors.", paper: SOURCE, page: 3 },
    answer: "拿查询和每个键做点积算相似度，除以根号维度后过 softmax 得到权重，再对值加权求和。除以根号维度是为了不让 softmax 太尖。",
    related: [{ kind: "前置", name: "Softmax" }, { kind: "相似", name: "Additive Attention" }],
  },
  {
    id: "c2", name: "Self-Attention", translation: "自注意力", level: 2, due: true, papers: 5,
    definition: { quote: "Self-attention, sometimes called intra-attention is an attention mechanism relating different positions of a single sequence in order to compute a representation of the sequence.", paper: SOURCE, page: 2 },
    answer: "同一个序列自己和自己做注意力，每个位置都能直接看到其他所有位置。",
    related: [{ kind: "前置", name: "Scaled Dot-Product Attention" }, { kind: "对比", name: "Recurrent Layer" }],
  },
  {
    id: "c3", name: "Multi-Head Attention", translation: "多头注意力", level: 2, due: true, papers: 4,
    definition: { quote: "Multi-head attention allows the model to jointly attend to information from different representation subspaces at different positions.", paper: SOURCE, page: 5 },
    answer: "把注意力分成几组并行做，每组看不同的子空间，最后拼起来。",
    related: [{ kind: "前置", name: "Self-Attention" }],
  },
  {
    id: "c4", name: "Positional Encoding", translation: "位置编码", level: 1, due: true, papers: 2,
    definition: { quote: "Since our model contains no recurrence and no convolution, in order for the model to make use of the order of the sequence, we must inject some information about the relative or absolute position of the tokens in the sequence.", paper: SOURCE, page: 6 },
    answer: null,
    related: [{ kind: "对比", name: "Learned Positional Embedding" }],
  },
  {
    id: "c5", name: "Residual Connection", translation: "残差连接", level: 2, due: false, papers: 6,
    definition: { quote: "We employ a residual connection [11] around each of the two sub-layers, followed by layer normalization [1].", paper: SOURCE, page: 3 },
    answer: "把子层的输入直接加到输出上，梯度多一条直通的路，层数深了也好训练。",
    related: [{ kind: "相似", name: "Highway Network" }],
  },
  {
    id: "c6", name: "Label Smoothing", translation: "标签平滑", level: 0, due: false, papers: 1,
    definition: null,
    answer: null,
    related: [],
  },
];

export const SAMPLE_NOTES: NoteEntry[] = [
  {
    id: "n1", paper: SOURCE, page: 2,
    quote: "The Transformer allows for significantly more parallelization and can reach a new state of the art in translation quality after being trained for as little as twelve hours on eight P100 GPUs.",
    body: "并行是主要卖点：我的数据序列更长，要先确认注意力的平方开销能不能接受。",
  },
  {
    id: "n2", paper: SOURCE, page: 4,
    quote: "We call our particular attention \"Scaled Dot-Product Attention\" (Figure 2).",
    body: null,
  },
];
