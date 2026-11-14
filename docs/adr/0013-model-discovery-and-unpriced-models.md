# ADR 0013：填好地址和密钥后自动列出可用模型；价格未知的模型按 token 上限控制

- 状态：已接受（2026-11-14，用户在“没价格就留空不计价 / 没价格仍让用户填 / 内置价格表”中选了第一项）
- 修订：ARCHITECTURE.md 第 15.2 节；docs/MODEL-SETUP.md

## 背景

模型配置要求用户手填模型名和价格（`MODEL-SETUP.md`：“价格不写死，由用户按官网价格页填写”），`options.prices` 缺模型时直接报配置错误。用户常常不知道准确的模型名，也找不到价格页，尤其是中转站和本地网关。

各家都有列模型的接口：OpenAI 兼容 `GET {base_url}/models`、Anthropic `GET /v1/models`、Gemini `GET /v1beta/models`。这些接口基本只返回模型名；少数聚合站（如 OpenRouter）会附带每 token 价格。

## 决定

1. 新增 `POST /api/settings/models/list`：接收协议、地址、密钥（或已保存提供方的密钥引用），调用该协议的列模型接口，返回 `[{id, input_price, output_price}]`，价格单位美元/百万 token，读不到时为 `null`。不调用生成接口，不计费，不写配置。失败时返回可读原因，前端退回手动输入。
2. 列模型属于 `LLM` 适配器的能力：各协议适配器实现 `list_models()`，价格解析只认响应里明确的价格字段（如 OpenRouter 的 `pricing.prompt` / `pricing.completion`），不猜测、不内置价格表。
3. 模型设置表单：填完地址和密钥后自动拉取列表，模型名改为可搜索的下拉框（仍允许手动输入列表外的名字）；选中的模型若带价格则自动填入，否则价格栏留空，标注“价格未知”，用户可选填。
4. `options.prices` 中的模型价格允许缺失或为 `null`，表示**价格未知**：
   - 调用照常进行，`llm_call` 记录 token，费用记为未知（`NULL`），不当作 0。
   - 美元预算（全局 `daily_usd`、任务 `daily_budget_usd`）只统计已知费用。
   - 新增 `budget.daily_tokens_unpriced`（默认 2,000,000，输入加输出）：价格未知的调用按 UTC 自然日合计 token，超出后与美元预算耗尽同样处理（推迟到下一个日界）。设为 0 表示禁止调用未知价格的模型。
   - 界面在费用处显示“未知（已用 N token）”。
5. 原有“tasks 用到的模型缺价格时启动报错”的规则取消；价格格式错误（负数、非数字）仍然报错。

## 后果

- 预算检查要同时看美元和未知价格 token 两条线；`llm_call.cost_usd` 已允许为空则无需迁移，否则新增迁移。
- 未知价格的模型只能按 token 量控制，无法给出美元估算；连接测试的 `estimated_usd` 对这类模型返回 `null`。
- 列模型接口可能很慢或分页（Gemini、Anthropic 有分页），按协议读取所有页，最多 1000 个。
