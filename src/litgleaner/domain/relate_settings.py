"""论文关系核实的完整输入和每侧连续原文预算。"""

from pydantic import Field

from litgleaner.domain.base import FrozenModel


class RelateSettings(FrozenModel):
    token_budget: int = Field(default=8000, strict=True, ge=512, le=64000)
    max_fragments_per_side: int = Field(default=4, strict=True, ge=1, le=16)
    fragment_chars: int = Field(default=1600, strict=True, ge=100, le=10000)
