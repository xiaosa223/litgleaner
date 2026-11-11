"""模型设置的校验、密钥引用和原子持久化；不发模型请求。"""

import os
import tempfile
import uuid
from contextlib import suppress
from dataclasses import replace
from pathlib import Path
from urllib.parse import urlsplit, urlunsplit

import yaml

from litgleaner.domain.errors import ConfigError, InvalidRequest
from litgleaner.infra.config import AdapterConfig, LLMBudget, LLMTask, LoadedConfig
from litgleaner.ports.model_settings import (
    ModelDraft,
    ModelProviderView,
    ModelSettingsView,
    ModelTask,
    ModelTaskView,
)
from litgleaner.ports.secrets import SecretStore

TASKS: tuple[ModelTask, ...] = ("triage", "extract", "assess", "translate", "ask")


class SecretOverlay:
    """校验草稿时不写钥匙串，也不临时改进程环境变量。"""

    def __init__(self, store: SecretStore, name: str | None, value: str | None) -> None:
        self._store, self._name, self._value = store, name, value

    def get(self, name: str) -> str | None:
        return self._value if name == self._name else self._store.get(name)

    def set(self, name: str, value: str) -> None:
        self._store.set(name, value)

    def delete(self, name: str) -> None:
        self._store.delete(name)


def _safe_url(value: object) -> str | None:
    if not isinstance(value, str):
        return None
    url = urlsplit(value)
    return urlunsplit((url.scheme, url.netloc.rsplit("@", 1)[-1], url.path, "", ""))


def _provider(name: str, provider: AdapterConfig, secrets: SecretStore) -> ModelProviderView:
    key = next((secrets.get(item) for item in provider.credentials_env), None)
    return ModelProviderView(
        name,
        provider.adapter,
        provider.enabled,
        _safe_url(provider.options.get("base_url")),
        bool(key),
        key[-4:] if key and len(key) > 4 else None,
        provider.adapter.startswith("fake_"),
    )


def settings_view(
    config: LoadedConfig, secrets: SecretStore, protocols: tuple[str, ...], ready: set[str]
) -> ModelSettingsView:
    providers = tuple(_provider(name, item, secrets) for name, item in config.llm.providers.items())
    by_name = {provider.name: provider for provider in providers}
    tasks = []
    for name in TASKS:
        task = config.llm.tasks.get(name)
        demonstration = bool(
            task and task.provider in by_name and by_name[task.provider].demonstration
        )
        tasks.append(
            ModelTaskView(
                name,
                task.provider if task else None,
                task.model if task else None,
                name in ready and not demonstration,
                demonstration,
            )
        )
    return ModelSettingsView(providers, tuple(tasks), protocols, config.llm.budget.daily_usd)


def prepare(
    config: LoadedConfig, draft: ModelDraft, protocols: tuple[str, ...]
) -> tuple[LoadedConfig, str | None]:
    if draft.protocol not in protocols or not draft.tasks:
        raise InvalidRequest("model_settings_invalid")
    used_by_others = [
        name
        for name, task in config.llm.tasks.items()
        if task.provider == draft.provider and name not in draft.tasks
    ]
    if used_by_others:
        raise InvalidRequest("model_provider_used_by_other_tasks")
    old = config.llm.providers.get(draft.provider)
    credential = f"LITGLEANER_MODEL_{uuid.uuid4().hex.upper()}" if draft.api_key else None
    credentials = (credential,) if credential else (old.credentials_env if old else ())
    options = dict(old.options) if old and old.adapter == draft.protocol else {}
    prices = dict(options.get("prices", {}))
    prices[draft.model] = [draft.input_price, draft.output_price]
    options.update(
        {
            "prices": prices,
            "max_output_tokens": draft.max_output_tokens,
            "structured": draft.structured,
        }
    )
    if draft.base_url:
        options["base_url"] = draft.base_url
    provider = AdapterConfig(adapter=draft.protocol, credentials_env=credentials, options=options)
    providers = {**config.llm.providers, draft.provider: provider}
    tasks = {
        **config.llm.tasks,
        **{
            name: LLMTask(
                provider=draft.provider,
                model=draft.model,
                prompt=config.llm.tasks[name].prompt if name in config.llm.tasks else name,
            )
            for name in draft.tasks
        },
    }
    settings = config.llm.model_copy(
        update={
            "providers": providers,
            "tasks": tasks,
            "budget": LLMBudget(daily_usd=draft.daily_budget_usd),
        }
    )
    return replace(config, llm=settings), credential


def _atomic_write(path: Path, content: bytes) -> None:
    descriptor, temporary = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    try:
        with os.fdopen(descriptor, "wb") as handle:
            handle.write(content)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
    finally:
        Path(temporary).unlink(missing_ok=True)


def _read_optional(path: Path) -> bytes | None:
    try:
        return path.read_bytes() if path.exists() else None
    except OSError:
        raise ConfigError("model_settings_read_failed") from None


class ModelConfigFile:
    def __init__(self, config: LoadedConfig, secrets: SecretStore) -> None:
        self.config, self.secrets = config, secrets
        self.path = config.settings_dir / "llm.yaml"
        self._expected = _read_optional(self.path)

    def commit(self, candidate: LoadedConfig, name: str | None, key: str | None) -> None:
        current = _read_optional(self.path)
        if current != self._expected:
            raise InvalidRequest("model_settings_changed_on_disk")
        content = yaml.safe_dump(candidate.llm.model_dump(mode="json"), allow_unicode=True).encode()
        try:
            if name and key:
                self.secrets.set(name, key)
            if current is not None:
                _atomic_write(self.path.with_suffix(".yaml.bak"), current)
            _atomic_write(self.path, content)
        except Exception:
            if name and key:
                with suppress(Exception):
                    self.secrets.delete(name)
            raise ConfigError("model_settings_save_failed") from None
        self._expected, self.config = content, candidate
