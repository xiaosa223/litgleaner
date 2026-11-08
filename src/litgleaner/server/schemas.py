"""请求体校验。在系统边界限制字段和长度，校验失败返回 422。"""

from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator

from litgleaner.domain.reading import ReadingStatus

_MAX_POSITION_KEYS = 8
_MAX_POSITION_TEXT = 64
_MAX_POSITION_NUMBER = 1_000_000
# 页面归一化坐标 0–1000（见 domain/reading.py）
Coordinate = Annotated[int, Field(ge=0, le=1000)]
Box = tuple[Coordinate, Coordinate, Coordinate, Coordinate]
_MAX_RECTS = 200


class _Body(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class ReadingBody(_Body):
    status: ReadingStatus
    position: dict[str, int | str] | None = None  # 例如 {"view": "pdf", "page": 3}

    @field_validator("position")
    @classmethod
    def _small_position(cls, value: dict[str, int | str] | None) -> dict[str, int | str] | None:
        if value is None:
            return None
        if len(value) > _MAX_POSITION_KEYS:
            raise ValueError("position 字段过多")
        if any(
            len(key) > _MAX_POSITION_TEXT
            or (isinstance(item, str) and len(item) > _MAX_POSITION_TEXT)
            or (isinstance(item, int) and abs(item) > _MAX_POSITION_NUMBER)
            for key, item in value.items()
        ):
            raise ValueError("position 内容过长或数值越界")
        return value


class AnnotationBody(_Body):
    kind: Literal["highlight", "note"]
    page: int | None = Field(default=None, ge=0, le=100_000)
    bbox: Box | None = None
    rects: list[Box] = Field(default_factory=list, max_length=_MAX_RECTS)
    quote: str | None = Field(default=None, max_length=5_000)
    body: str | None = Field(default=None, max_length=20_000)


class AnnotationEdit(_Body):
    body: str | None = Field(default=None, max_length=20_000)


class TranslateBody(_Body):
    """按页或整篇翻译：要翻译的块序号。"""

    indices: list[Annotated[int, Field(ge=0, le=1_000_000)]] = Field(
        min_length=1, max_length=20_000
    )
