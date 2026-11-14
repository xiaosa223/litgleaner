"""用户上传的文档（任务书、开题报告、比赛文档）→ 纯文字，用于推断研究画像（ADR 0012）。

失败统一抛 InvalidRequest，消息是固定错误码：
document_type_unsupported、document_no_text、document_too_large、document_unreadable。
"""

from typing import Protocol


class DocumentText(Protocol):
    def extract(self, name: str, content: bytes) -> str: ...
