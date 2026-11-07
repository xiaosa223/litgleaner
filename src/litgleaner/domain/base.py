"""领域模型基类：不可变，并拒绝未知字段。"""

from pydantic import BaseModel, ConfigDict


class FrozenModel(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")
