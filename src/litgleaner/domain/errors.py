"""统一错误类型。调度器按类型决定是否重试，不解析错误文字。"""


class LitGleanerError(Exception):
    """所有业务错误的基类。"""


class PermanentError(LitGleanerError):
    """重试不会成功，任务直接进入死信并告警。"""


class AuthFailed(PermanentError):
    """凭据无效或已过期。"""


class SchemaChanged(PermanentError):
    """外部返回结构与预期不符，需要修改适配器。"""


class TransientError(LitGleanerError):
    """临时故障，可以按退避策略重试。"""


DEFAULT_RETRY_AFTER_SECONDS = 3600


class Deferred(TransientError):
    """现在做不了，但不是失败：推迟到指定时间后再试，不计失败次数。"""

    def __init__(
        self, message: str, retry_after_seconds: int = DEFAULT_RETRY_AFTER_SECONDS
    ) -> None:
        super().__init__(message)
        self.retry_after_seconds = retry_after_seconds


class RateLimited(Deferred):
    """触发外部频率限制或额度用完。"""


class StillProcessing(Deferred):
    """外部任务还没完成，下次继续查询，不重新提交。"""


class BudgetExceeded(LitGleanerError):
    """超出 LLM 每日预算。任务保持待处理，第二天继续，不计失败次数。"""


class NotFound(LitGleanerError):
    """要找的论文、批注或文件不存在。"""


class ConfigError(LitGleanerError):
    """配置错误，启动时直接报告。"""
