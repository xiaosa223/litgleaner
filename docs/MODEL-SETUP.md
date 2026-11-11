# 模型原地配置后端

本地服务提供模型配置接口，供首次模型操作的原地配置界面调用。界面接线仍待实现。现有命令行 YAML 配置继续可用；缺少 `llm.yaml` 时按未配置启动，阅读 PDF、批注和本地检索照常可用。

所有接口沿用启动会话、Host/Origin 和 CSRF 检查。响应不返回密钥全文，校验错误不回显请求体。

| 接口 | 行为 |
|---|---|
| `GET /api/settings/models` | 返回提供方、任务、预算和可用协议；任务配置状态与演示适配器分开；密钥只显示已设置与末四位 |
| `POST /api/settings/models/test` | 草稿参数发送固定短内容；后台任务验证结构化输出，翻译验证流式输出，按所选任务测试对应能力；返回结果和费用，不写配置或钥匙串 |
| `PUT /api/settings/models` | 校验后保存配置和密钥，后续请求及下一轮调度使用新配置；不调用模型 |

测试与保存接收相同参数：

状态中的 `ready` 表示任务路由已构建；连接测试单独返回本次实际协议响应的能力结果。

```json
{
  "provider": "personal",
  "protocol": "openai_chat",
  "model": "YOUR_MODEL",
  "tasks": ["translate"],
  "base_url": "http://127.0.0.1:3425/v1",
  "api_key": "YOUR_KEY",
  "input_price": 0,
  "output_price": 0,
  "daily_budget_usd": 2,
  "max_output_tokens": 4000,
  "structured": "native"
}
```

价格单位是美元/百万 token，须填提供方实际价格。协议由状态接口返回，目前支持四种 API 协议。`tasks` 只允许已实现的 `triage`、`extract`、`assess`、`translate`、`ask`、`map`，只启用明确选中的任务。同一提供方若还被其他未选任务使用，须选择新名称，或把需要同步修改的任务都选上。同协议编辑保留 YAML 中的兼容参数和自定义提示词。没有 key 的本地网关可省略 `api_key`；省略已有提供方的 key 表示沿用密钥引用。

连接测试会实际调用模型并计费，每次调用前检查当日预算，已知费用写入 `llm_call`。失败响应提供了用量时也记账；提供方未返回用量时无法确认实际费用。返回的 `estimated_usd` 是短测试的经验估计。测试使用当前已保存的预算；保存新的预算后才影响后续调用。

密钥写入系统钥匙串：macOS Keychain、Windows WinVault、Linux SecretService。没有可用钥匙串时，环境变量仍可读取，写入明确失败；不会回退到明文文件。环境变量优先于钥匙串。YAML 只存密钥名，原配置精确备份到 `llm.yaml.bak` 后原子替换；写入失败保留旧配置和旧运行时，清理本次新建密钥。检测到用户从磁盘修改配置时拒绝覆盖。

正在执行的请求和 SSE 固定使用开始时的运行时快照。保存后新请求取新快照，原请求继续完成；历史提取、评估和译文不会因保存而自动重算。

缺少模型时，HTTP 返回 `409` 和 `code=model_configuration_required`、`task`；预算用完返回 `402` 和 `code=budget_exceeded`。SSE 错误同样包含稳定 `code` 和原有 `message`。后台任务保持 pending，缺模型不计重试次数；配置完成后可继续执行。
