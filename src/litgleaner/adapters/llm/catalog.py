"""读取各协议的“列出模型”接口（ADR 0013）。

只读响应里明确的价格字段（OpenRouter 风格的 `pricing.prompt` / `pricing.completion`，
单位美元 / token），不内置价格表、不猜测；缺失或格式不对时价格为 None（未知）。
"""

import math
from collections.abc import Callable, Iterator, Mapping
from typing import Any

from litgleaner.domain.errors import SchemaChanged
from litgleaner.ports.llm import ModelInfo

MAX_MODELS = 1000
MAX_PAGES = 50
_PER_MILLION = 1_000_000

GetJson = Callable[[str, Mapping[str, Any] | None], Any]


def _per_million(value: object) -> float | None:
    if isinstance(value, bool) or not isinstance(value, str | int | float):
        return None
    try:
        number = float(value)
    except ValueError:
        return None
    if not math.isfinite(number) or number < 0:
        return None
    return round(number * _PER_MILLION, 6)


def _prices(item: Mapping[str, Any]) -> tuple[float | None, float | None]:
    pricing = item.get("pricing")
    if not isinstance(pricing, Mapping):
        return None, None
    prompt, completion = (
        _per_million(pricing.get("prompt")),
        _per_million(pricing.get("completion")),
    )
    if prompt is None or completion is None:
        return None, None
    return prompt, completion


def _openai_pages(get: GetJson) -> Iterator[list[Any]]:
    data = get("/models", None)
    yield _items(data, "data")


def _anthropic_pages(get: GetJson) -> Iterator[list[Any]]:
    params: dict[str, Any] = {"limit": MAX_MODELS}
    while True:
        data = get("/v1/models", params)
        yield _items(data, "data")
        if not (data.get("has_more") is True and isinstance(data.get("last_id"), str)):
            return
        params = {**params, "after_id": data["last_id"]}


def _gemini_pages(get: GetJson) -> Iterator[list[Any]]:
    params: dict[str, Any] = {"pageSize": MAX_MODELS}
    while True:
        data = get("/models", params)
        yield [
            {**item, "id": str(item["name"]).removeprefix("models/")}
            for item in _items(data, "models")
            if isinstance(item, Mapping)
            and isinstance(item.get("name"), str)
            and "generateContent" in (item.get("supportedGenerationMethods") or ())
        ]
        token = data.get("nextPageToken")
        if not (isinstance(token, str) and token):
            return
        params = {**params, "pageToken": token}


def _items(data: Any, key: str) -> list[Any]:
    if not isinstance(data, Mapping) or not isinstance(data.get(key), list):
        raise SchemaChanged(f"列模型接口返回的结构与预期不符：缺少 {key} 列表")
    return list(data[key])


PAGERS: Mapping[str, Callable[[GetJson], Iterator[list[Any]]]] = {
    "openai_chat": _openai_pages,
    "openai_responses": _openai_pages,
    "anthropic_messages": _anthropic_pages,
    "gemini": _gemini_pages,
}


def read_catalog(protocol: str, get: GetJson) -> tuple[ModelInfo, ...]:
    """按协议翻页读取，最多 MAX_PAGES 页、MAX_MODELS 个，按出现顺序去重。"""
    found: dict[str, ModelInfo] = {}
    for number, items in enumerate(PAGERS[protocol](get)):
        for item in items:
            model_id = item.get("id") if isinstance(item, Mapping) else None
            if isinstance(model_id, str) and model_id.strip() and model_id not in found:
                found[model_id] = ModelInfo(model_id, *_prices(item))
            if len(found) >= MAX_MODELS:
                return tuple(found.values())
        if number + 1 >= MAX_PAGES:
            break
    return tuple(found.values())
