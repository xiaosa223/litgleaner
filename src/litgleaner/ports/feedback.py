"""用户反馈只追加，关系反馈保留明确目标及动作数据。"""

from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any


@dataclass(frozen=True)
class FeedbackRecord:
    id: str
    paper_id: str
    channel: str
    action: str
    digest_id: str | None = None
    actor: str | None = None
    external_id: str | None = None
    relation_id: str | None = None
    payload: Mapping[str, Any] | None = None
