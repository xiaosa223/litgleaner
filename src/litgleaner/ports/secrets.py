"""密钥访问接口（第 15.4 节）。读取时环境变量优先，写入只进系统钥匙串。

不可用或操作失败时抛 ConfigError，文本是稳定错误码，不带密钥名和密钥值。
"""

from typing import Protocol


class SecretStore(Protocol):
    def get(self, name: str) -> str | None: ...

    def set(self, name: str, value: str) -> None: ...

    def delete(self, name: str) -> None: ...
