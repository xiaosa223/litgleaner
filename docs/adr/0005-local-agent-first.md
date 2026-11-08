# ADR 0005：优先复用用户本机的 agent

- 状态：已接受（2026-11-08，按推荐方案）；第 6、8 条被 [ADR 0008](0008-agent-for-ask-only.md) 取代，第 2 条的安全前提经实测不成立，见 ADR 0008
- 修订：REQUIREMENTS.md R20、R24；ARCHITECTURE.md 第 12.1、12.4、15.2、15.3、15.5 节

## 背景

- 用户不愿为模型 API 付费，按 token 计费会让他们“心痛”（REQUIREMENTS P8）。很多学生和研究者手上已经有 Codex、Claude Code 的订阅，或者装了 DeepSeek Harness。
- 第 15.2 节只支持 API 提供方，需要用户填 key、按 token 付费；第 12.4 节写明“内置问答……不需要安装任何 agent”；R24 把接入 agent 列为 P2、证据弱。
- Agent Client Protocol（ACP）是编辑器和 agent 之间的开放协议：客户端以子进程启动 agent，通过 stdio 上的 JSON-RPC 通信。Codex、Claude Code 有官方维护的 ACP 适配器，DeepSeek Harness 自带 ACP 模式。Agentero 用它接入了十几种 agent，其 `docs/backend/agent.md` 记录了大量各家差异和平台问题，可作为避坑清单。

调研时核实的版本（2026-11-08）：

| agent | 启动命令 | 来源 | 付费方式 |
|---|---|---|---|
| Codex | `codex-acp` | npm `@agentclientprotocol/codex-acp` 2.5.0，Apache-2.0；另需 `codex` CLI 并已登录 | 用户的 ChatGPT 订阅或 OpenAI key |
| Claude Code | `claude-agent-acp` | npm `@agentclientprotocol/claude-agent-acp` 0.101.0，Apache-2.0；另需 `claude` CLI 并已登录 | 用户的 Claude 订阅或 Anthropic key |
| DeepSeek Harness | `dsh --profile acp` | npm `@deepseek-ai/dsh`（MIT），0.1.2 起内置 ACP 模式 | 用户自己的 `DEEPSEEK_API_KEY`（据 Agentero 文档，本机未核实）：便宜，但不是免费 |

## 决定

1. **本机 agent 是一类模型提供方。** `llm.yaml` 的 `providers` 增加协议 `acp`，按模板提供 `codex`、`claude_code`、`deepseek_harness` 三个预设，另允许自定义 `command`、`args`。它实现现有的 `LLM` 接口（`complete` 和 `stream`），流程代码、网关、提示词都不感知它是 agent：
   - `complete`：结构写进提示词（等同现有的 `structured: prompt`），回答文本由网关按完整 JSON Schema 校验，不合格时有限重试。
   - `stream`：把 agent 的消息增量（`agent_message_chunk`）转成 `TextDelta`，翻译照常走 ADR 0004 的校验。
2. **agent 只当模型用，不给任何工具权限。**
   - 初始化时不声明文件读写和终端能力；agent 发来的每个权限请求（`session/request_permission`）一律拒绝。
   - 每次调用在应用数据目录下的一个空目录里开会话，不在用户的项目或文献目录里启动。
   - agent 提供只读或计划模式时（会话返回的 `modes` 里有），切到该模式。
   - 不挂任何 MCP 服务。以后给对话侧栏挂只读上下文时另行决定。
   - 论文内容照常用分隔标记包裹、视为不可信；回答照常过原文核验（第 12.1 节）。
3. **进程与会话。**
   - 常驻服务为每个启用的 agent 提供方保持一个已初始化的进程，按需启动，空闲一段时间后关闭。
   - 每次调用新开一个会话，调用结束即关闭，避免不同论文、不同任务之间串上下文。
   - 同一提供方同一时间只跑一个调用，其余排队。用户在领读器里发起的操作排在后台任务前面（第 15.2 节）。
   - 超时：初始化 30 秒，开会话 30 秒，单次调用按任务配置。用户取消时发 `session/cancel`。
   - 平台：Windows 上 npm 安装的命令是 `.cmd` 包装，要按 Windows 方式启动，退出时结束整个进程树；macOS、Linux 从图形界面启动时继承不到 shell 配置，要合并登录 shell 的环境变量（PATH、代理、key）。
4. **额度取代美元。** agent 通道按 token 计价没有意义：价格记为 0，记录调用次数和估算的 token（`StreamDone.estimated`）。每个 agent 提供方可以设每日调用上限，界面显示“本机 agent：今日 N 次”。第 12.1 节的“花费看得见”改为“花费和额度看得见”。
5. **拾穗不替用户安装 agent。** 首次向导和 `litgleaner doctor` 检测 `codex`、`claude`、`dsh` 和对应适配器是否可用，是否已登录；缺什么就显示安装和登录命令（可复制）。适配器是 npm 包，用户本机需要 Node.js；安装包不打包 Node 和 agent。
6. **首次向导的顺序改为：先检测本机 agent，没有再引导填 API key。** 推荐低价模型的说明保留。
7. **协议实现自己写一个同步的最小客户端**，放在 `adapters/llm/` 下，不引入官方 Python SDK。理由：项目代码是同步的，SDK 基于 asyncio；SDK 稳定版 0.12.1，1.0 仍在候选版本，接口还在变；我们只用初始化、开会话、发提示、接收更新、拒绝权限、取消这几个方法。录制三家 agent 的真实 JSON-RPC 往来，存到 `tests/fixtures/acp/`，过统一契约测试。

8. **本机 agent 只接领读器里用户主动发起的任务**：`translate`（含用户点开的整篇翻译）、`ask`、`explain`、`guide`、`evaluate`。后台流水线任务（`triage`、`extract`、`assess`、`topic_draft`、`review`，以及推送论文的后台预翻译）不走 agent，由配置校验拒绝把这些任务路由到 `acp` 提供方。理由：每天几十到几百次后台调用会很快用完订阅额度，挤占用户自己的使用；冷启动慢；各家订阅条款是否允许这种自动化调用没有核实。后台任务用 API 提供方（推荐低价模型）；没有配置 API 时，后台流水线的 LLM 阶段保持待处理，不报错。

## 实施前核实

- 订阅条款：Claude、ChatGPT 订阅被第三方客户端经 ACP 调用的条款。
- 各家 agent 是否在 ACP 消息里报告 token 用量；不报告时一律按文本长度估算。
- DeepSeek Harness 的 ACP 模式是否确实读 `DEEPSEEK_API_KEY`，在本机实测。

## 后果

- REQUIREMENTS：R20 改为“优先复用本机 agent，其次低价 API”；R24 从 P2 升为 P0，依据记“产品决定”。
- ARCHITECTURE：第 12.4 节内置问答可以走本机 agent；第 15.2 节新增 `acp` 协议和额度规则；第 15.3 节数据去向加“本机 agent 及其背后的模型提供方”；第 15.5 节首次向导顺序调整。
- agent 冷启动要几秒到几十秒，“首字 2 秒内出现”（第 12.1 节）只在进程已预热时可能做到，需要实测。
- 每家 agent 的版本升级都可能改行为，契约测试和 `doctor` 要能发现；各家差异只写在适配器的模板里，不进流程代码。
