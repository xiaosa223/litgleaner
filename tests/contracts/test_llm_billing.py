"""HTTP 已返回用量时，坏输出仍携带账单；未知用量不能伪造免费记录。"""

from copy import deepcopy

import httpx
import pytest
from tests.contracts.test_llm_protocols import CASES, PRICES, REQUEST

from litgleaner.domain.errors import LitGleanerError, PermanentError, SchemaChanged, TransientError

_USAGE_FIELD = {"gemini": "usageMetadata"}


def _chat_failure(body: dict, failure: str) -> None:
    body["choices"][0]["message"]["content"] = "invalid JSON"
    if failure == "refusal":
        body["choices"][0]["message"]["refusal"] = "不能回答"
    elif failure == "truncated":
        body["choices"][0]["finish_reason"] = "length"
    elif failure == "shape":
        body["choices"] = []


def _failure_body(protocol: str, failure: str) -> dict:
    body = deepcopy(CASES[protocol][2])
    if protocol == "chat":
        _chat_failure(body, failure)
    elif protocol == "responses":
        body["output"] = [
            {"type": "message", "content": [{"type": "output_text", "text": "invalid JSON"}]}
        ]
        if failure == "refusal":
            body["output"][0]["content"] = [{"type": "refusal", "refusal": "不能回答"}]
        elif failure == "truncated":
            body.update(status="incomplete", incomplete_details={"reason": "max_output_tokens"})
        elif failure == "shape":
            body["output"] = None
    elif protocol == "anthropic":
        body["content"] = [{"type": "text", "text": "invalid JSON"}]
        if failure in {"refusal", "truncated"}:
            body["stop_reason"] = "refusal" if failure == "refusal" else "max_tokens"
        elif failure == "shape":
            body["content"] = None
    else:
        body["candidates"][0]["content"]["parts"] = [{"text": "invalid JSON"}]
        if failure in {"refusal", "truncated"}:
            body["candidates"][0]["finishReason"] = (
                "SAFETY" if failure == "refusal" else "MAX_TOKENS"
            )
        elif failure == "shape":
            body["candidates"] = []
    return body


def _complete(protocol: str, body: dict):
    cls = CASES[protocol][0]
    transport = httpx.MockTransport(lambda _request: httpx.Response(200, json=body))
    llm = cls("main", {"prices": PRICES}, None, transport=transport)
    try:
        return llm.complete(REQUEST)
    finally:
        llm.close()


@pytest.mark.parametrize("protocol", CASES)
@pytest.mark.parametrize(
    "failure,error_type,message",
    [
        ("json", TransientError, "不是 JSON"),
        ("refusal", PermanentError, "拒绝回答"),
        ("truncated", TransientError, "max_output_tokens"),
        ("shape", SchemaChanged, "结构与预期不符"),
    ],
)
def test_failed_output_keeps_error_contract_and_known_usage(
    protocol: str, failure: str, error_type: type[LitGleanerError], message: str
):
    with pytest.raises(error_type, match=message) as caught:
        _complete(protocol, _failure_body(protocol, failure))

    assert type(caught.value) is error_type
    usage = caught.value.model_usage
    assert usage is not None
    assert usage.model == CASES[protocol][2].get("modelVersion", CASES[protocol][2].get("model"))
    assert (usage.input_tokens, usage.output_tokens) == (1000, 500)
    assert usage.cost_usd == pytest.approx(0.003)


@pytest.mark.parametrize("protocol", CASES)
@pytest.mark.parametrize("failure", ["json", "refusal", "truncated", "shape"])
@pytest.mark.parametrize("usage_value", [None, {}, {"unknown": 0}])
def test_unknown_usage_does_not_turn_into_a_zero_bill(protocol: str, failure: str, usage_value):
    body = _failure_body(protocol, failure)
    field = _USAGE_FIELD.get(protocol, "usage")
    if usage_value is None:
        body.pop(field)
    else:
        body[field] = usage_value
    with pytest.raises(LitGleanerError) as caught:
        _complete(protocol, body)
    assert caught.value.model_usage is None


@pytest.mark.parametrize("protocol", CASES)
def test_reported_zero_tokens_remain_known_usage(protocol: str):
    body = _failure_body(protocol, "refusal")
    field = _USAGE_FIELD.get(protocol, "usage")
    body[field] = dict.fromkeys(body[field], 0)
    with pytest.raises(PermanentError) as caught:
        _complete(protocol, body)
    assert caught.value.model_usage is not None
    assert caught.value.model_usage.cost_usd == 0.0


@pytest.mark.parametrize("protocol", CASES)
@pytest.mark.parametrize("bad_count", [-1, 1.5, "10", True, None])
def test_malformed_usage_does_not_hide_the_original_refusal(protocol: str, bad_count):
    body = _failure_body(protocol, "refusal")
    field = _USAGE_FIELD.get(protocol, "usage")
    token = {"chat": "prompt_tokens", "gemini": "promptTokenCount"}.get(protocol, "input_tokens")
    body[field][token] = bad_count
    with pytest.raises(PermanentError, match="拒绝回答") as caught:
        _complete(protocol, body)
    assert caught.value.model_usage is None


def test_anthropic_failure_includes_reported_cache_tokens():
    body = _failure_body("anthropic", "truncated")
    body["usage"].update(cache_creation_input_tokens=200, cache_read_input_tokens=300)
    with pytest.raises(TransientError) as caught:
        _complete("anthropic", body)
    assert caught.value.model_usage.input_tokens == 1500
    assert caught.value.model_usage.cost_usd == pytest.approx(0.0035)
