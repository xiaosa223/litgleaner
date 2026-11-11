"""本地模型设置接口；密钥只接收写入，响应不回显输入或第三方错误。"""

from typing import Literal, cast
from urllib.parse import urlsplit

from fastapi import FastAPI, Request
from pydantic import Field, SecretStr, field_validator

from litgleaner.application import model_setup
from litgleaner.domain.errors import InvalidRequest
from litgleaner.ports.model_settings import ModelDraft, ModelSettings, ModelSettingsView, ModelTask
from litgleaner.server.runtime import Context
from litgleaner.server.schemas import _Body


class ModelBody(_Body):
    provider: str = Field(pattern=r"^[a-z][a-z0-9_-]{0,47}$")
    protocol: str = Field(min_length=1, max_length=80)
    model: str = Field(min_length=1, max_length=200, pattern=r"^\S(?:[^\r\n]*\S)?$")
    tasks: list[ModelTask] = Field(min_length=1, max_length=7)
    input_price: float = Field(ge=0, le=1_000_000, allow_inf_nan=False)
    output_price: float = Field(ge=0, le=1_000_000, allow_inf_nan=False)
    api_key: SecretStr | None = Field(default=None, min_length=1, max_length=4096)
    base_url: str | None = Field(default=None, max_length=2000)
    daily_budget_usd: float = Field(default=2, ge=0, le=1_000_000, allow_inf_nan=False)
    max_output_tokens: int = Field(default=4000, ge=128, le=200_000)
    structured: Literal["native", "prompt"] = "native"

    @field_validator("base_url")
    @classmethod
    def _endpoint(cls, value: str | None) -> str | None:
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

    def draft(self) -> ModelDraft:
        data = self.model_dump(exclude={"api_key", "tasks"})
        return ModelDraft(
            **data,
            tasks=tuple(dict.fromkeys(self.tasks)),
            api_key=self.api_key.get_secret_value() if self.api_key else None,
        )


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

    @app.put("/api/settings/models")
    def save_model(request: Request, body: ModelBody) -> ModelSettingsView:
        return model_setup.save(_settings(request), body.draft())
