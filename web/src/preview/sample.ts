// 界面预览用的示例数据：知识库的掌握程度、复习等还没有接口，这里的内容只用来看界面，页面上会标明“示例数据”。
// 论文都是公开的经典论文；数量、分组、回答等数字和文字是编的。作者原话逐字取自样本论文
// （arXiv 1706.03762），页码已核对。后端接上后删除本目录（见 ROADMAP 的已知差异）。
import type { Concept, NoteEntry } from "../types";

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
