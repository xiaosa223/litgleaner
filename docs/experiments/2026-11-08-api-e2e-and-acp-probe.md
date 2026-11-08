# API 端到端与本机 agent 实测（2026-11-08）

两件事：用一个 OpenAI 兼容接口把流水线和段落翻译真实跑一遍；按 ADR 0005 录制本机 agent 的 ACP 往来，核实“只当模型用”能否做到。结论推动了 [ADR 0008](../adr/0008-agent-for-ask-only.md)。

## 1. API 端到端

### 环境

- 提供方：本机的 OpenAI 兼容网关（Magpie，`http://127.0.0.1:3425/v1`），协议 `openai_chat`。
- 模型：`workbuddy-ai/hy4free`（网关报价为 0，推理模型，响应里带 `reasoning_content`）。
- 数据：仓库自带的演示配置（假信号源、假全文、假解析），把 `llm.yaml` 的四个任务全部改到这个提供方。配置副本放在仓库外，没有改仓库里的配置。

### 结果

| 阶段 | 调用 | 成功 | 输入 token | 输出 token | 说明 |
|---|---|---|---|---|---|
| triage | 5 | 5 | 2,341 | 3,708 | |
| extract | 3 | 3 | 1,943 | 18,343 | 第一轮 1 次被截断，调大输出上限后通过 |
| assess | 10 | 4 | 12,084 | 107,277 | 失败 6 次，原因见下 |
| translate（单段） | 1 | 1 | | | 译文通过句数和术语校验；首字 20.8 秒 |

流水线从 discover 跑到 score 全部完成。演示论文的正文只有一两句话，4 篇都被判为 drop，所以没有走到 select、publish（这一段不涉及 LLM）。

评估阶段每篇约 2 分钟，第一轮整个流程用了 9 分 42 秒。

### 发现的问题

1. **接口不强制执行 JSON Schema。** 以 `structured: native` 发送 `response_format: json_schema` 时，模型多次返回裸数组 `[{"key": "relevance", ...}]`，而不是 `{"dimensions": [...]}`，网关报“返回的 JSON 不是对象”或“缺少 dimensions”。改成 `structured: prompt` 后 4 篇评估全部通过。网关本地的完整校验按设计拦住了这些输出，没有写入错误数据。
2. **思考 token 挤占输出上限。** 默认 `max_output_tokens: 4000` 不够，8000 时仍有截断，16000 时全部通过。`reasoning_effort`、`reasoning.effort`、`thinking.type: disabled`、`enable_thinking: false` 四种写法对思考长度都没有明显影响（思考 222～415 token，问题相同）。
3. **推理模型首字慢。** 流式输出先给出思考内容（`reasoning_content`），适配器只取正文，用户在前 20 秒左右看不到任何东西。第 12.1 节“首字 2 秒内出现”用这类模型做不到。界面至少要显示“模型思考中”，或者为翻译选不带思考的模型。
4. 模型识别出了演示数据里故意放的注入文字（`[[level:4]]`），在理由里写明“文中含有试图影响评分的文字”。

### 能用的配置

```yaml
providers:
  compat:
    adapter: openai_chat
    enabled: true
    credentials_env: [COMPAT_API_KEY]
    options:
      base_url: http://127.0.0.1:3425/v1
      prices: { workbuddy-ai/hy4free: [0, 0] }
      max_output_tokens: 16000
      max_tokens_field: max_tokens
      structured: prompt
```

## 2. 本机 agent（ACP）

### 环境

| agent | 版本 | 状态 |
|---|---|---|
| DeepSeek Harness | `dsh` 0.2.1-rc.2，`dsh --profile acp` | 已实测 |
| Codex | `codex-cli` 0.166.0；适配器 `@agentclientprotocol/codex-acp` 未安装 | 未实测 |
| Claude Code | 2.1.319；适配器 `@agentclientprotocol/claude-agent-acp` 未安装 | 未实测 |

客户端按 ADR 0005 第 2 条初始化：`fs.readTextFile`、`fs.writeTextFile`、`terminal` 都声明为 false；收到 `session/request_permission` 一律回答 `cancelled`；会话目录是一个空目录；不挂 MCP。

### DeepSeek Harness 的往来

- `initialize` 约 1 秒；返回 `sessionCapabilities` 含 `close`、`list`、`resume`，没有 `loadSession`。
- `session/new` 约 0.1 秒；没有 `modes`，模型通过 `configOptions` 选择。
- 简单翻译提示 1.3 秒返回，正文通过 `agent_message_chunk` 给出。
- 用量只有 `usage_update`（`used` 是上下文占用，约 7,700 token，含 agent 自带的系统提示；`size` 是上下文窗口），**没有每次调用的输入输出 token**。只能按文本长度估算。

### 权限实测

提示词要求“在当前目录建 hello.txt，再列出 /Users”：

| 模式 | 写文件 | 读 /Users | 权限请求 |
|---|---|---|---|
| 默认（`workspace-write`） | 成功写入会话目录 | 成功 | 0 次 |
| `DSH_PERMISSION_MODE=read-only` | 被沙箱拦下；提权请求被拒绝 | 成功 | 1 次（提权），已拒绝 |

工具调用以 `tool_call`（`status: in_progress`）和 `tool_call_update`（`completed`）通知客户端，客户端收到时工具已经在执行。

另外试了用 `--patch` 关掉工具插件（`tool-bash`、`tool-fs` 等写 `disabled: true`），`--dump-config` 显示没有生效，补丁格式需要另查。

### 结论

- “不声明能力 + 拒绝权限请求”不能保证 agent 没有工具。至少读文件在三家的只读模式下都不需要询问（后两家按公开资料，未实测）。
- 用户决定：先用 API 跑通；本机 agent 只用于边读边问（ADR 0008）。接入时的安全措施另行决定。

## 3. 按段落分批翻译（同日，新实现）

用 MinerU 解析的《Attention Is All You Need》（arXiv 1706.03762v7），翻译第 1、2 页的 10 段，提供方和模型同第 1 节（输出上限 16000）。

| 项 | 结果 |
|---|---|
| 段落合并 | 156 块中 64 块要翻译，合并出 0 个跨块段落。逐个核对了末尾没有句号的 9 块：都以公式结尾、下一块以大写开头，确实不是同一段。MinerU 的 VLM 结果已经把跨栏、跨页段落合并好了；合并规则由单元测试覆盖 |
| “思考中” | 1.9 秒出现 |
| 整批（10 段，约一页） | 思考约 150 秒后输出被截断，一段也没写完 |
| 逐段重试 | 10 段全部通过校验，平均每段约 77 秒；第一句译文在第 309 秒出现 |
| 总耗时、调用 | 924 秒，11 次调用（1 次整批 + 10 次逐段） |
| 占位符 | 引用编号 `[13]`、`[35, 2, 5]` 和公式 `$h _ { t }$` 都原样还原到译文的正确位置 |

结论：

- 推理模型配一页一批，思考加译文会超过输出上限。截断后已改为保留写完的段落、其余逐段重试，所以最终全部译出，但前面那一批的时间白等了。
- 这个免费推理模型做整页翻译太慢（每段一分多钟）。翻译应推荐不带思考的模型；如果要支持推理模型，可以把批大小做成按模型配置，或者截断后对半拆分重试，而不是直接逐段。这一点待决定。
- 分句器把 “position t. This …” 当作一句（单个字母加句点视为人名缩写），译文也按一句翻译，对齐没有问题。

## 未做

- Codex、Claude Code 的往来录制：需要用户自行安装 ACP 适配器（拾穗不替用户安装）。
- 订阅条款核实（ADR 0005“实施前核实”第 1 项）。
