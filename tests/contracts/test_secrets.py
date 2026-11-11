"""密钥端口合同。所有操作只触达内存后端，绝不打开真实系统钥匙串。"""

import sys
import traceback
from types import ModuleType, SimpleNamespace

import keyring
import pytest
from keyring.errors import KeyringError, NoKeyringError, PasswordDeleteError

from litgleaner.adapters.secrets import SystemSecretStore, system
from litgleaner.domain.errors import ConfigError
from litgleaner.ports.secrets import SecretStore

NAME = "LITGLEANER_TEST_SECRET"
VALUE = "private-test-value-not-a-real-key"
_SELECT_BACKEND = system._system_backend


class MemoryBackend:
    priority = 1

    def __init__(self):
        self.values = {}
        self.calls = []
        self.error = None

    def get_password(self, service, name):
        self.calls.append(("get", service, name))
        if self.error:
            raise self.error
        return self.values.get((service, name))

    def set_password(self, service, name, value):
        self.calls.append(("set", service, name))
        if self.error:
            raise self.error
        self.values[service, name] = value

    def delete_password(self, service, name):
        self.calls.append(("delete", service, name))
        if self.error:
            raise self.error
        self.values.pop((service, name), None)


@pytest.fixture(autouse=True)
def no_real_keyring(monkeypatch):
    monkeypatch.delenv(NAME, raising=False)
    monkeypatch.setattr(system, "_system_backend", lambda: None)
    for name in ("get_keyring", "get_password", "set_password", "delete_password"):
        monkeypatch.setattr(keyring, name, lambda *_a, **_kw: pytest.fail("禁止自动钥匙串操作"))


@pytest.fixture
def backend(monkeypatch):
    memory = MemoryBackend()
    monkeypatch.setattr(system, "_system_backend", lambda: memory)
    return memory


def test_environment_wins_and_does_not_initialize_keyring(monkeypatch):
    monkeypatch.setenv(NAME, VALUE)
    monkeypatch.setattr(system, "_system_backend", lambda: pytest.fail("环境变量无需访问钥匙串"))
    store: SecretStore = SystemSecretStore()
    assert store.get(NAME) == VALUE


def test_empty_environment_falls_back_and_environment_changes_apply(backend, monkeypatch):
    backend.values[system.SERVICE_NAME, NAME] = "stored"
    monkeypatch.setenv(NAME, "")
    store = SystemSecretStore()
    assert store.get(NAME) == "stored"
    monkeypatch.setenv(NAME, VALUE)
    assert store.get(NAME) == VALUE
    monkeypatch.delenv(NAME)
    assert store.get(NAME) == "stored"


def test_round_trip_uses_only_explicit_system_backend(backend, monkeypatch, tmp_path):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("PYTHON_KEYRING_BACKEND", "keyrings.alt.file.PlaintextKeyring")
    store: SecretStore = SystemSecretStore()
    assert store.get(NAME) is None
    store.set(NAME, VALUE)
    assert backend.values == {(system.SERVICE_NAME, NAME): VALUE}
    assert store.get(NAME) == VALUE
    store.delete(NAME)
    assert store.get(NAME) is None
    assert [call[0] for call in backend.calls].count("set") == 1
    assert list(tmp_path.iterdir()) == []  # 不生成明文文件、配置或后备存储


def test_writes_and_deletes_do_not_change_environment(backend, monkeypatch):
    monkeypatch.setenv(NAME, "environment-wins")
    store = SystemSecretStore()
    store.set(NAME, VALUE)
    assert store.get(NAME) == "environment-wins"
    store.delete(NAME)
    assert store.get(NAME) == "environment-wins"
    assert backend.values == {}


def test_missing_delete_is_idempotent(backend):
    store = SystemSecretStore()
    store.delete(NAME)
    store.delete(NAME)
    assert not any(call[0] == "delete" for call in backend.calls)


def test_no_backend_keeps_environment_reads_available_but_rejects_writes(monkeypatch):
    store = SystemSecretStore()
    assert store.get(NAME) is None
    monkeypatch.setenv(NAME, VALUE)
    assert store.get(NAME) == VALUE
    with pytest.raises(ConfigError, match=r"^secret_store_unavailable$"):
        store.set(NAME, VALUE)
    with pytest.raises(ConfigError, match=r"^secret_store_unavailable$"):
        store.delete(NAME)


def test_backend_reporting_no_keyring_is_treated_as_unavailable_on_read(backend):
    backend.error = NoKeyringError(VALUE)
    assert SystemSecretStore().get(NAME) is None


@pytest.mark.parametrize("error_type", [KeyringError, RuntimeError, OSError])
@pytest.mark.parametrize(
    "operation,code", [("get", "read"), ("set", "write"), ("delete", "delete")]
)
def test_failures_have_stable_messages_without_secret_or_backend_exception(
    backend, caplog, capsys, *, error_type, operation, code
):
    backend.error = error_type(f"{NAME}: {VALUE}")
    store = SystemSecretStore()
    with pytest.raises(ConfigError, match=f"^secret_store_{code}_failed$") as caught:
        if operation == "set":
            store.set(NAME, VALUE)
        else:
            getattr(store, operation)(NAME)
    rendered = "".join(traceback.format_exception(caught.value))
    assert VALUE not in rendered and NAME not in str(caught.value)
    assert caught.value.__cause__ is None and caught.value.__suppress_context__
    assert VALUE not in repr(store) and VALUE not in caplog.text
    output = capsys.readouterr()
    assert VALUE not in output.out + output.err


def test_delete_race_is_idempotent_only_when_recheck_confirms_missing(backend, monkeypatch):
    backend.values[system.SERVICE_NAME, NAME] = VALUE

    def already_removed(service, name):
        backend.values.pop((service, name))
        raise PasswordDeleteError(VALUE)

    monkeypatch.setattr(backend, "delete_password", already_removed)
    SystemSecretStore().delete(NAME)
    assert backend.values == {}


def test_delete_failure_is_not_silenced_when_secret_is_still_present(backend, monkeypatch):
    backend.values[system.SERVICE_NAME, NAME] = VALUE

    def refused(_service, _name):
        raise PasswordDeleteError(VALUE)

    monkeypatch.setattr(backend, "delete_password", refused)
    with pytest.raises(ConfigError, match=r"^secret_store_delete_failed$"):
        SystemSecretStore().delete(NAME)
    assert backend.values[system.SERVICE_NAME, NAME] == VALUE


def test_delete_race_recheck_failure_is_redacted(backend, monkeypatch):
    def read(_service, _name):
        if getattr(backend, "removed", False):
            raise KeyringError(VALUE)
        return VALUE

    def refused(_service, _name):
        backend.removed = True
        raise PasswordDeleteError(VALUE)

    monkeypatch.setattr(backend, "get_password", read)
    monkeypatch.setattr(backend, "delete_password", refused)
    with pytest.raises(ConfigError, match=r"^secret_store_delete_failed$") as caught:
        SystemSecretStore().delete(NAME)
    assert VALUE not in "".join(traceback.format_exception(caught.value))


@pytest.mark.parametrize(
    "platform,module_name,class_name",
    [
        ("darwin", "keyring.backends.macOS", "Keyring"),
        ("win32", "keyring.backends.Windows", "WinVaultKeyring"),
        ("linux", "keyring.backends.SecretService", "Keyring"),
    ],
)
def test_platform_selects_allowlisted_backend_without_auto_discovery(
    monkeypatch, platform, module_name, class_name
):
    module = ModuleType(module_name)
    setattr(module, class_name, MemoryBackend)
    monkeypatch.setitem(sys.modules, module_name, module)
    monkeypatch.setattr(system, "sys", SimpleNamespace(platform=platform))
    assert isinstance(_SELECT_BACKEND(), MemoryBackend)


def test_unknown_platform_has_no_fallback(monkeypatch):
    monkeypatch.setattr(system, "sys", SimpleNamespace(platform="unknown"))
    assert _SELECT_BACKEND() is None


@pytest.mark.parametrize("failure", [ImportError, RuntimeError, KeyringError])
def test_missing_platform_dependencies_disable_keyring_without_leaking(monkeypatch, failure):
    def unavailable():
        raise failure(VALUE)

    monkeypatch.setattr(system, "_backend_class", unavailable)
    assert _SELECT_BACKEND() is None


def test_nonsecure_backend_priority_is_not_accepted(monkeypatch):
    class Disabled(MemoryBackend):
        priority = 0

    monkeypatch.setattr(system, "_backend_class", lambda: Disabled)
    assert _SELECT_BACKEND() is None


@pytest.mark.parametrize("name", ["", "bad\0name", "bad\nname", "x" * 129])
def test_invalid_names_fail_without_touching_the_keyring(backend, name):
    with pytest.raises(ConfigError, match=r"^secret_store_invalid_name$"):
        SystemSecretStore().get(name)
    assert backend.calls == []


def test_empty_secret_is_rejected_without_backend_writes(backend):
    with pytest.raises(ConfigError, match=r"^secret_store_empty_value$"):
        SystemSecretStore().set(NAME, "")
    assert backend.calls == []
