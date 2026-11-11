"""系统密钥存储适配器；不启用文件后端或自动选择的第三方后端。"""

from litgleaner.adapters.secrets.system import SystemSecretStore

__all__ = ["SystemSecretStore"]
