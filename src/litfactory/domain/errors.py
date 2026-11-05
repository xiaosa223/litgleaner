"""统一错误类型。调度器按类型决定是否重试，不解析错误文字。"""


class LitFactoryError(Exception):
    """所有业务错误的基类。"""


class PermanentError(LitFactoryError):
    """重试不会成功，任务直接进入死信并告警。"""


class AuthFailed(PermanentError):
    """凭据无效或已过期。"""


class SchemaChanged(PermanentError):
    """外部返回结构与预期不符，需要修改适配器。"""


class TransientError(LitFactoryError):
    """临时故障，可以按退避策略重试。"""


class RateLimited(TransientError):
    """触发外部频率限制。"""


class BudgetExceeded(LitFactoryError):
    """超出 LLM 每日预算。任务保持待处理，第二天继续，不计失败次数。"""


class ConfigError(LitFactoryError):
    """配置错误，启动时直接报告。"""
