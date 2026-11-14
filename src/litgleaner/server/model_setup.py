"""本地模型设置接口；密钥只接收写入，响应不回显输入或第三方错误。"""

from dataclasses import dataclass
from typing import Annotated, Literal, cast, get_args
from urllib.parse import urlsplit

from fastapi import FastAPI, Request
from pydantic import Field, SecretStr, field_validator, model_validator

from litgleaner.application import model_setup
from litgleaner.domain.errors import InvalidRequest
from litgleaner.ports.llm import ModelInfo
from litgleaner.ports.model_settings import (
    CatalogDraft,
    MaxTokensField,
    ModelDraft,
    ModelSettings,
    ModelSettingsView,
    ModelTask,
)
from litgleaner.server.runtime import Context
from litgleaner.server.schemas import _Body

# 只有 Chat Completions 风格的协议用这两个字段名；其他协议各有自己的字段
_MAX_TOKENS_FIELD_PROTOCOLS = frozenset({"openai_chat"})
_TaskBudget = Annotated[float, Field(ge=0, strict=True, allow_inf_nan=False)]


def _endpoint(value: str | None) -> str | None:
    if value is None:
        return None
    url = urlsplit(value)
    if (
        url.scheme not in {"http", "https"}
        or not url.hostname
        or url.username
        or url.password
        or url.query
        or url.fragment
    ):
        raise ValueError("model_endpoint_invalid")
    return value.rstrip("/")


class ModelBody(_Body):
    provider: str = Field(pattern=r"^[a-z][a-z0-9_-]{0,47}$")
    protocol: str = Field(min_length=1, max_length=80)
    model: str = Field(min_length=1, max_length=200, pattern=r"^\S(?:[^\r\n]*\S)?$")
    tasks: list[ModelTask] = Field(min_length=1, max_length=len(get_args(ModelTask)))
    # None：价格未知（ADR 0013），按每日 token 上限控制
    input_price: float | None = Field(default=None, ge=0, le=1_000_000, allow_inf_nan=False)
    output_price: float | None = Field(default=None, ge=0, le=1_000_000, allow_inf_nan=False)
    api_key: SecretStr | None = Field(default=None, min_length=1, max_length=4096)
    base_url: str | None = Field(default=None, max_length=2000)
    daily_budget_usd: float = Field(default=2, ge=0, le=1_000_000, allow_inf_nan=False)
    max_output_tokens: int = Field(default=4000, ge=128, le=200_000)
    structured: Literal["native", "prompt"] = "native"
    max_tokens_field: MaxTokensField | None = None
    task_budgets_usd: dict[ModelTask, _TaskBudget | None] = Field(default_factory=dict)

    @model_validator(mode="after")
    def _selected_budgets(self) -> "ModelBody":
        if set(self.task_budgets_usd) - set(self.tasks):
            raise ValueError("model_task_budget_invalid")
        if self.max_tokens_field and self.protocol not in _MAX_TOKENS_FIELD_PROTOCOLS:
            raise ValueError("model_max_tokens_field_invalid")
        return self

    @field_validator("base_url")
    @classmethod
    def _endpoint(cls, value: str | None) -> str | None:
        return _endpoint(value)

    def draft(self) -> ModelDraft:
        data = self.model_dump(exclude={"api_key", "tasks"})
        return ModelDraft(
            **data,
            tasks=tuple(dict.fromkeys(self.tasks)),
            api_key=self.api_key.get_secret_value() if self.api_key else None,
        )


class CatalogBody(_Body):
    provider: str = Field(pattern=r"^[a-z][a-z0-9_-]{0,47}$")
    protocol: str = Field(min_length=1, max_length=80)
    api_key: SecretStr | None = Field(default=None, min_length=1, max_length=4096)
    base_url: str | None = Field(default=None, max_length=2000)

    @field_validator("base_url")
    @classmethod
    def _endpoint(cls, value: str | None) -> str | None:
        return _endpoint(value)

    def source(self) -> CatalogDraft:
        key = self.api_key.get_secret_value() if self.api_key else None
        return CatalogDraft(self.provider, self.protocol, key, self.base_url)


@dataclass(frozen=True)
class CatalogResult:
    models: tuple[ModelInfo, ...]
    code: str | None = None


def _settings(request: Request) -> ModelSettings:
    settings = cast(ModelSettings | None, request.app.state.model_settings)
    if settings is None:
        raise InvalidRequest("model_settings_unavailable")
    return settings


def install_model_routes(app: FastAPI) -> None:
    @app.get("/api/settings/models")
    def models(request: Request) -> ModelSettingsView:
        return model_setup.status(_settings(request))

    @app.post("/api/settings/models/test")
    def test_model(request: Request, ctx: Context, body: ModelBody) -> model_setup.ProbeResult:
        return model_setup.probe(ctx, _settings(request), body.draft())

    @app.post("/api/settings/models/list")
    def list_models(request: Request, body: CatalogBody) -> CatalogResult:
        settings = _settings(request)
        try:
            return CatalogResult(model_setup.list_models(settings, body.source()))
        except InvalidRequest:
            raise
        except Exception:
            # 提供方错误可能回显认证头；界面只收到固定错误码，退回手动输入
            return CatalogResult((), "model_list_failed")

    @app.put("/api/settings/models")
    def save_model(request: Request, body: ModelBody) -> ModelSettingsView:
        return model_setup.save(_settings(request), body.draft())
