"""Zotero 信号源和全文来源共用的选项。"""

from pathlib import Path
from typing import Literal, Self

from pydantic import Field, model_validator

from litgleaner.adapters.common.options import FullTextOptions, SourceOptions
from litgleaner.domain.base import FrozenModel
from litgleaner.domain.errors import ConfigError

API_KEY_ENV = "ZOTERO_API_KEY"


class LibraryOptions(FrozenModel):
    """local 读本机数据目录；web 读 user_id 或 group_id 对应的在线文献库。"""

    mode: Literal["local", "web"] = "local"
    data_dir: Path | None = None
    user_id: int | None = Field(default=None, gt=0)
    group_id: int | None = Field(default=None, gt=0)
    base_url: str = "https://api.zotero.org"

    @model_validator(mode="after")
    def _check_library(self) -> Self:
        if self.mode == "web" and (self.user_id is None) == (self.group_id is None):
            raise ValueError("web 模式需要设置 user_id 或 group_id 之一（不能同时设置）")
        return self

    @property
    def library_path(self) -> str:
        """Web API 的库前缀，同时用作 web 模式外部 ID 的前缀：users/123 或 groups/456。"""
        if self.group_id is not None:
            return f"groups/{self.group_id}"
        return f"users/{self.user_id}"


class ZoteroSourceOptions(SourceOptions, LibraryOptions):
    # None 表示首次运行导入全部历史；默认只取最近 30 天，避免第一次把整个库灌进流程。
    lookback_days: int | None = Field(default=30, ge=0, le=36500)  # type: ignore[assignment]
    collection: str | None = Field(default=None, min_length=1)
    page_size: int = Field(default=100, gt=0, le=100)
    min_interval_seconds: float = Field(default=0.5, ge=0)


class ZoteroFullTextOptions(FullTextOptions, LibraryOptions):
    min_interval_seconds: float = Field(default=0.5, ge=0)
    max_candidates: int = Field(default=10, gt=0, le=100)


def require_api_key(options: LibraryOptions, api_key: str | None, label: str) -> str | None:
    """web 模式必须有 key；local 模式不需要，也不使用。"""
    if options.mode != "web":
        return None
    if not api_key:
        raise ConfigError(f"{label}：Zotero web 模式需要环境变量 {API_KEY_ENV}")
    return api_key
