"""适配器配置校验。配置写错在启动时报出来，而不是跑到一半才失败。"""

from collections.abc import Callable, Mapping
from datetime import UTC, date, datetime
from typing import Any

from pydantic import Field, ValidationError

from litfactory.domain.base import FrozenModel
from litfactory.domain.errors import ConfigError

Today = Callable[[], date]


def utc_today() -> date:
    return datetime.now(UTC).date()


class SourceOptions(FrozenModel):
    """信号源通用选项。各适配器继承后再加自己的字段。"""

    lookback_days: int = Field(default=7, ge=0, le=3650)
    overlap_days: int = Field(default=3, ge=0, le=365)
    max_records: int = Field(default=500, gt=0, le=10_000)
    min_interval_seconds: float = Field(default=1.0, ge=0)
    timeout_seconds: float = Field(default=30, gt=0)


class FullTextOptions(FrozenModel):
    min_interval_seconds: float = Field(default=1.0, ge=0)
    timeout_seconds: float = Field(default=30, gt=0)
    max_pdf_mb: int = Field(default=80, gt=0, le=500)
    # 代理 fake-IP 模式（Clash、Surge 等）下域名都解析到 198.18.0.0/15，关掉后这些环境无法下载。
    allow_proxy_fake_ip: bool = True


def parse_options[OptionsT: FrozenModel](
    model: type[OptionsT], raw: Mapping[str, Any], label: str
) -> OptionsT:
    try:
        return model.model_validate(dict(raw))
    except ValidationError as error:
        raise ConfigError(f"{label} 配置无效：{error}") from error
