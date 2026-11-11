"""环境变量 + 系统钥匙串；显式挑选安全后端，绝不回退到明文文件。

环境变量在每次读取时重新检查，优先于钥匙串。选择后端是惰性的：只用环境变量
的命令行不会访问系统钥匙串，也不会弹出授权窗口。无后端时读取返回 None，写入报错。
"""

import os
import re
import sys
from importlib import import_module
from typing import cast

from keyring.backend import KeyringBackend
from keyring.errors import NoKeyringError, PasswordDeleteError

from litgleaner.domain.errors import ConfigError

SERVICE_NAME = "LitGleaner"
_NAME = re.compile(r"^[A-Za-z_][A-Za-z0-9_]{0,127}$")
_BACKENDS = {
    "darwin": ("keyring.backends.macOS", "Keyring"),
    "win32": ("keyring.backends.Windows", "WinVaultKeyring"),
    "linux": ("keyring.backends.SecretService", "Keyring"),
}


def _backend_class() -> type[KeyringBackend] | None:
    platform = "linux" if sys.platform.startswith("linux") else sys.platform
    target = _BACKENDS.get(platform)
    if target is None:
        return None
    module_name, class_name = target
    return cast(type[KeyringBackend], getattr(import_module(module_name), class_name))


def _system_backend() -> KeyringBackend | None:
    try:
        backend_class = _backend_class()
        if backend_class is None or backend_class.priority <= 0:
            return None
        return backend_class()
    except Exception:
        # 缺系统依赖、无会话总线或系统钥匙串不可用。不能调用自动发现以寻找替代后端。
        return None


def _validate_name(name: str) -> None:
    if _NAME.fullmatch(name) is None:
        raise ConfigError("secret_store_invalid_name")


class SystemSecretStore:
    def __init__(self) -> None:
        self._resolved = False
        self._keyring: KeyringBackend | None = None

    def _backend(self) -> KeyringBackend | None:
        if not self._resolved:
            self._keyring = _system_backend()
            self._resolved = True
        return self._keyring

    def _writable(self) -> KeyringBackend:
        backend = self._backend()
        if backend is None:
            raise ConfigError("secret_store_unavailable")
        return backend

    def get(self, name: str) -> str | None:
        _validate_name(name)
        if value := os.environ.get(name):
            return value
        backend = self._backend()
        if backend is None:
            return None
        try:
            return backend.get_password(SERVICE_NAME, name) or None
        except NoKeyringError:
            return None
        except Exception:
            raise ConfigError("secret_store_read_failed") from None

    def set(self, name: str, value: str) -> None:
        _validate_name(name)
        if not value:
            raise ConfigError("secret_store_empty_value")
        backend = self._writable()
        try:
            backend.set_password(SERVICE_NAME, name, value)
        except Exception:
            raise ConfigError("secret_store_write_failed") from None

    def delete(self, name: str) -> None:
        _validate_name(name)
        backend = self._writable()
        try:
            if backend.get_password(SERVICE_NAME, name) is not None:
                backend.delete_password(SERVICE_NAME, name)
        except PasswordDeleteError:
            # 不把取消、锁定等删除失败当成“已不存在”；只有复查确实缺失才幂等成功。
            self._confirm_missing(backend, name)
        except Exception:
            raise ConfigError("secret_store_delete_failed") from None

    @staticmethod
    def _confirm_missing(backend: KeyringBackend, name: str) -> None:
        try:
            if backend.get_password(SERVICE_NAME, name) is None:
                return
        except Exception:
            pass
        raise ConfigError("secret_store_delete_failed") from None
