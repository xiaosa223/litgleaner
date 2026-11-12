"""组装层的适配器参数和凭据检查；具体实现注册仍在 bootstrap。"""

import os
from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from litgleaner.adapters.common.capture import configured_options
from litgleaner.adapters.secrets import SystemSecretStore
from litgleaner.domain.errors import ConfigError
from litgleaner.ports.secrets import SecretStore
from litgleaner.ports.source_capture import SourceRecipe

CONTACT_EMAIL = "LITGLEANER_CONTACT_EMAIL"


@dataclass(frozen=True)
class BuildArgs:
    name: str
    options: Mapping[str, Any]
    root: Path
    credentials: Mapping[str, str]

    optional: frozenset[str] = frozenset()
    secrets: SecretStore | None = None
    seen: dict[str, bool] = field(default_factory=dict, repr=False, compare=False)
    contact_email: str | None = field(
        default_factory=lambda: os.environ.get(CONTACT_EMAIL) or None, init=False
    )

    def secret(self, env_name: str) -> str:
        """先读环境变量，再读系统钥匙串；配置文件里只写密钥名。"""
        if env_name not in self.credentials:
            raise ConfigError(f"{self.name} 需要在 credentials_env 中声明 {env_name}")
        value = (self.secrets or SystemSecretStore()).get(env_name) or ""
        self.seen[env_name] = bool(value)
        return value

    def optional_secret(self, env_name: str) -> str | None:
        """可选的 key 或联系邮箱：没有也能运行，只是额度更低。必须先在配置里声明。"""
        if env_name not in self.credentials and env_name not in self.optional:
            raise ConfigError(f"{self.name} 需要在 optional_env 中声明 {env_name}")
        value = (self.secrets or SystemSecretStore()).get(env_name) or None
        self.seen[env_name] = bool(value)
        return value

    def options_with_paths(self, *keys: str) -> dict[str, Any]:
        """把配置里的相对路径换成项目根目录下的绝对路径。"""
        resolved = dict(self.options)
        for key in keys:
            if key in resolved:
                resolved[key] = str(self.root / str(resolved[key]))
        return resolved

    @property
    def contact(self) -> str | None:
        """礼貌通道用的联系邮箱，所有来源共用，不是密钥，不需要逐个声明。"""
        return self.contact_email

    def path(self, option: str) -> Path:
        return self.root / str(self.options[option])

    def optional_path(self, option: str) -> Path | None:
        return self.path(option) if option in self.options else None


def source_recipe(source: object, adapter: str, args: BuildArgs) -> SourceRecipe:
    configured = configured_options(source)
    options, fields = configured or (dict(args.options), tuple(sorted(args.options)))
    return SourceRecipe(adapter, options, fields, dict(args.seen), args.contact)


def _llm_key(args: BuildArgs) -> str | None:
    """credentials_env 写 0 个（本地服务）或 1 个变量名。声明了就必须有值，启动时报错。"""
    if len(args.credentials) > 1:
        raise ConfigError(f"大模型提供方 {args.name} 的 credentials_env 只能写一个变量")
    env = next(iter(args.credentials), None)
    if env is None:
        return None
    key = args.secret(env)
    if not key:
        raise ConfigError(f"大模型提供方 {args.name} 需要环境变量 {env}")
    return key
