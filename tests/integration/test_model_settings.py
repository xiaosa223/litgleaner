"""模型配置持久化：真实 YAML 和原子替换，密钥存储仅用内存。"""

import traceback
from pathlib import Path

import pytest
import yaml
from tests.e2e.conftest import _write_config

from litgleaner.domain.errors import ConfigError, InvalidRequest
from litgleaner.infra import model_settings
from litgleaner.infra.config import load_config
from litgleaner.infra.model_settings import ModelConfigFile, prepare
from litgleaner.ports.model_settings import ModelDraft

KEY = "private-memory-key-never-written-to-yaml"
PROTOCOLS = ("openai_chat",)


class MemorySecrets:
    def __init__(self):
        self.values = {"EXISTING_KEY": "keep-existing"}
        self.calls = []
        self.fail_set = False

    def get(self, name):
        return self.values.get(name)

    def set(self, name, value):
        self.calls.append(("set", name))
        self.values[name] = value
        if self.fail_set:
            raise RuntimeError(KEY)

    def delete(self, name):
        self.calls.append(("delete", name))
        self.values.pop(name, None)


def draft(**changes):
    return ModelDraft(
        **{
            "provider": "personal",
            "protocol": "openai_chat",
            "model": "configured-model",
            "tasks": ("translate",),
            "input_price": 1,
            "output_price": 3,
            "api_key": KEY,
            "base_url": "https://models.example.test/v1",
            **changes,
        }
    )


@pytest.fixture
def config_file(tmp_path):
    _write_config(tmp_path)
    return ModelConfigFile(load_config(tmp_path / "config"), MemorySecrets())


def test_commit_is_reloadable_preserves_other_tasks_and_backs_up_exact_previous_file(config_file):
    file = config_file
    previous_secrets = dict(file.secrets.values)
    previous = file.path.read_bytes()
    old_tasks = file.config.llm.tasks
    candidate, name = prepare(file.config, draft(), PROTOCOLS)
    file.commit(candidate, name, KEY)
    loaded = load_config(file.path.parent)
    assert file.path.with_suffix(".yaml.bak").read_bytes() == previous
    assert loaded.llm.tasks["translate"].provider == "personal"
    assert loaded.llm.tasks["triage"] == old_tasks["triage"]
    assert loaded.llm.tasks["assess"] == old_tasks["assess"]
    assert loaded.llm.providers["personal"].credentials_env == (name,)
    assert file.secrets.values[name] == KEY
    assert KEY not in file.path.read_text(encoding="utf-8")
    assert KEY not in file.path.with_suffix(".yaml.bak").read_text(encoding="utf-8")
    assert file.secrets.values["EXISTING_KEY"] == previous_secrets["EXISTING_KEY"]


@pytest.mark.parametrize("missing", [True, False])
def test_first_model_save_without_existing_providers_enables_only_selected_tasks(tmp_path, missing):
    _write_config(tmp_path)
    path = tmp_path / "config" / "llm.yaml"
    path.unlink() if missing else path.write_text("{}\n", encoding="utf-8")
    file = ModelConfigFile(load_config(path.parent), MemorySecrets())
    candidate, name = prepare(file.config, draft(), PROTOCOLS)
    file.commit(candidate, name, KEY)
    loaded = load_config(path.parent)
    assert set(loaded.llm.tasks) == {"translate"}
    assert set(loaded.llm.providers) == {"personal"}
    assert file.path.with_suffix(".yaml.bak").exists() is not missing
    assert file.secrets.values[name] == KEY


@pytest.mark.parametrize("failing_target", ["llm.yaml", "llm.yaml.bak"])
def test_atomic_replace_failure_preserves_config_and_removes_new_secret(
    config_file, monkeypatch, failing_target
):
    file = config_file
    previous, original = file.path.read_bytes(), file.config
    previous_secrets = dict(file.secrets.values)
    candidate, name = prepare(file.config, draft(), PROTOCOLS)
    actual_replace = model_settings.os.replace

    def failed_replace(source, target):
        if target.name == failing_target:
            raise OSError(KEY)
        return actual_replace(source, target)

    monkeypatch.setattr(model_settings.os, "replace", failed_replace)
    with pytest.raises(ConfigError, match=r"^model_settings_save_failed$") as caught:
        file.commit(candidate, name, KEY)
    assert file.path.read_bytes() == previous and file.config is original
    assert file.secrets.values == previous_secrets
    assert ("delete", name) in file.secrets.calls
    assert not list(file.path.parent.glob(".llm.yaml.*"))
    assert KEY not in "".join(traceback.format_exception(caught.value))


def test_secret_store_partial_failure_rolls_back_without_writing_config(config_file):
    file = config_file
    previous, original = file.path.read_bytes(), file.config
    previous_secrets = dict(file.secrets.values)
    file.secrets.fail_set = True
    candidate, name = prepare(file.config, draft(), PROTOCOLS)
    with pytest.raises(ConfigError, match=r"^model_settings_save_failed$"):
        file.commit(candidate, name, KEY)
    assert file.path.read_bytes() == previous and file.config is original
    assert file.secrets.values == previous_secrets
    assert file.secrets.calls == [("set", name), ("delete", name)]
    assert not file.path.with_suffix(".yaml.bak").exists()


def test_external_config_edit_is_rejected_before_any_secret_write(config_file):
    file = config_file
    original = file.config
    candidate, name = prepare(file.config, draft(), PROTOCOLS)
    external = file.path.read_bytes() + b"\n# user edit after page opened\n"
    file.path.write_bytes(external)
    with pytest.raises(InvalidRequest, match=r"^model_settings_changed_on_disk$"):
        file.commit(candidate, name, KEY)
    assert file.path.read_bytes() == external and file.config is original
    assert file.secrets.calls == []


def test_current_file_read_failure_is_redacted_before_secret_write(config_file, monkeypatch):
    file = config_file
    previous, original = file.path.read_bytes(), file.config
    candidate, name = prepare(file.config, draft(), PROTOCOLS)
    actual_read = Path.read_bytes

    def failed_read(path):
        if path == file.path:
            raise OSError(KEY)
        return actual_read(path)

    with monkeypatch.context() as patch:
        patch.setattr(Path, "read_bytes", failed_read)
        with pytest.raises(ConfigError, match=r"^model_settings_read_failed$") as caught:
            file.commit(candidate, name, KEY)
    assert KEY not in "".join(traceback.format_exception(caught.value))
    assert file.path.read_bytes() == previous and file.config is original
    assert file.secrets.calls == []


def test_same_protocol_edit_preserves_compatibility_options_prices_and_custom_prompt(config_file):
    path = config_file.path
    raw = yaml.safe_load(path.read_text(encoding="utf-8"))
    options = {
        "base_url": "https://legacy.models.test/v1",
        "max_tokens_field": "max_tokens",
        "extra_body": {"thinking": {"type": "disabled"}},
        "headers": {"X-Compatibility": "legacy"},
        "prices": {"previous-model": [0.2, 0.4]},
        "timeout_seconds": 42,
    }
    raw["providers"]["personal"] = {
        "adapter": "openai_chat",
        "options": options,
        "credentials_env": ["EXISTING_KEY"],
    }
    raw["tasks"]["translate"] = {
        "provider": "personal",
        "model": "previous-model",
        "prompt": "translate-custom",
    }
    path.write_text(yaml.safe_dump(raw), encoding="utf-8")
    file = ModelConfigFile(load_config(path.parent), config_file.secrets)
    original = file.config
    candidate, name = prepare(original, draft(base_url=None, api_key=None), PROTOCOLS)
    file.commit(candidate, name, None)
    loaded = load_config(path.parent)
    updated = loaded.llm.providers["personal"]
    for field in ("base_url", "max_tokens_field", "extra_body", "headers", "timeout_seconds"):
        assert updated.options[field] == options[field]
    assert updated.options["prices"] == {"previous-model": [0.2, 0.4], "configured-model": [1, 3]}
    assert updated.credentials_env == ("EXISTING_KEY",) and file.secrets.calls == []
    assert loaded.llm.tasks["translate"].prompt == "translate-custom"
    assert loaded.llm.tasks["translate"].model == "configured-model"
    for task in ("triage", "extract", "assess"):
        assert loaded.llm.tasks[task] == original.llm.tasks[task]
    assert original.llm.providers["personal"].options == options
    assert original.llm.tasks["translate"].model == "previous-model"


def test_non_default_config_directory_is_the_only_write_destination(tmp_path):
    _write_config(tmp_path)
    alternate = tmp_path / "researcher-settings"
    (tmp_path / "config").rename(alternate)
    file = ModelConfigFile(load_config(alternate), MemorySecrets())
    assert file.path == alternate / "llm.yaml"
    candidate, name = prepare(file.config, draft(), PROTOCOLS)
    file.commit(candidate, name, KEY)
    saved = yaml.safe_load((alternate / "llm.yaml").read_text(encoding="utf-8"))
    assert saved["tasks"]["translate"]["provider"] == "personal"
    assert not (tmp_path / "config").exists()
    assert (alternate / "llm.yaml.bak").exists()


def test_updating_provider_used_by_other_tasks_requires_explicit_selection(config_file):
    with pytest.raises(InvalidRequest, match=r"^model_provider_used_by_other_tasks$"):
        prepare(config_file.config, draft(provider="main", tasks=("translate",)), PROTOCOLS)
    assert config_file.secrets.calls == []
