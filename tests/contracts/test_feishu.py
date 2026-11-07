"""飞书：卡片渲染、发送与更新的错误分类、按钮回调的核验。

SDK 的请求和回调对象按 lark-oapi 1.7.5 的源码构造（2026-11 核对）；没有对真实飞书发过请求。
错误码分类是开放平台文档的常见值，属于记忆值，未逐个核实。
"""

import json
from types import SimpleNamespace
from typing import Any

import pytest
import requests

from litgleaner.adapters.feishu.api import FeishuMessenger, card_response, send_uuid
from litgleaner.adapters.feishu.card import MAX_CARD_BYTES, FeishuCardRenderer, build_card
from litgleaner.adapters.feishu.listener import (
    TOAST_FORBIDDEN,
    TOAST_INVALID,
    FeishuListener,
    answerers_from,
    parse_click,
)
from litgleaner.adapters.feishu.publisher import FeishuPublisher
from litgleaner.domain.digest import DigestView, PaperCard, to_public_card
from litgleaner.domain.errors import (
    AuthFailed,
    ConfigError,
    PermanentError,
    RateLimited,
    TransientError,
)
from litgleaner.ports.publisher import Rendered

ME = "ou_me"


def card(**overrides: Any) -> PaperCard:
    values: dict[str, Any] = {
        "paper_id": "01PAPER",
        "title": "Graph **bold** [click](https://evil.example) <at id=all></at>",
        "authors": ("Ada", "Bob", "Cy", "Dan"),
        "venue": "TNNLS",
        "published_at": "2026-10-01",
        "doi": "10.1000/a b",
        "score": 86,
        "tier": "must_read",
        "tier_label": "必读",
        "provisional": False,
        "summary": "总结" * 400,
        "conclusion": None,
        "limitations": ("样本少",),
        "innovation": "新方法",
        "abstract": None,
        "quotes": (),
        "fulltext_used": True,
        "parse_quality": "ok",
    }
    return PaperCard(**{**values, **overrides})


def view(*cards: Any, visibility: str = "private", **kw: Any) -> DigestView:
    return DigestView("01DIGEST", "EEG", "2026-11-07", visibility, tuple(cards), **kw)


def elements(data: dict[str, Any]) -> list[dict[str, Any]]:
    return data["body"]["elements"]


def buttons(data: dict[str, Any]) -> list[dict[str, Any]]:
    return [
        column["elements"][0]
        for element in elements(data)
        if element["tag"] == "column_set"
        for column in element["columns"]
    ]


def test_private_card_has_save_and_dismiss_buttons_with_short_values():
    data = build_card(view(card()))
    assert data["schema"] == "2.0" and data["config"]["update_multi"] is True
    save, dismiss, doi = buttons(data)
    assert save["text"]["content"] == "加入 Zotero"
    assert save["behaviors"] == [
        {"type": "callback", "value": {"a": "save", "p": "01PAPER", "d": "01DIGEST"}}
    ]
    assert dismiss["behaviors"][0]["value"]["a"] == "dismiss"
    assert doi["behaviors"] == [
        {"type": "open_url", "default_url": "https://doi.org/10.1000/a%20b"}
    ]


def test_untrusted_text_is_plain_text_and_clipped():
    data = build_card(view(card()))
    texts = [e["text"] for e in elements(data) if e["tag"] == "div"]
    assert all(text["tag"] == "plain_text" for text in texts)
    assert not [e for e in elements(data) if e["tag"] == "markdown"]
    summary = next(t["content"] for t in texts if t["content"].startswith("总结"))
    assert len(summary) <= 310 and summary.endswith("…")


def test_public_card_has_no_buttons_and_states_replace_buttons():
    assert buttons(build_card(view(to_public_card(card()), visibility="public"))) == []
    saved = build_card(view(card(), states={"01PAPER": "saved"}))
    assert buttons(saved) == []
    assert any(
        "已加入 Zotero" in e["text"]["content"] for e in elements(saved) if e["tag"] == "div"
    )


def test_oversized_card_is_a_permanent_error():
    many = [card(paper_id=f"P{i}", summary="长" * 300, innovation="新" * 300) for i in range(40)]
    with pytest.raises(PermanentError, match=str(MAX_CARD_BYTES)):
        FeishuCardRenderer().render(view(*many))


class FakeIm:
    def __init__(self, *responses: Any) -> None:
        self.responses = list(responses)
        self.requests: list[Any] = []

    def _next(self, request: Any) -> Any:
        self.requests.append(request)
        result = self.responses.pop(0)
        if isinstance(result, Exception):
            raise result
        return result

    create = _next
    patch = _next


def response(code: int = 0, message_id: str | None = "om_1") -> Any:
    return SimpleNamespace(
        success=lambda: code == 0,
        code=code,
        msg="error" if code else "ok",
        data=SimpleNamespace(message_id=message_id),
        get_log_id=lambda: "log123",
    )


def messenger(*responses: Any) -> tuple[FeishuMessenger, FakeIm]:
    im = FakeIm(*responses)
    client = SimpleNamespace(im=SimpleNamespace(v1=SimpleNamespace(message=im)))
    return FeishuMessenger("", "", client=client), im


def test_send_uses_stable_uuid_and_returns_message_id():
    sender, im = messenger(response())
    assert sender.send("open_id", ME, '{"a":1}') == "om_1"
    (request,) = im.requests
    assert request.receive_id_type == "open_id"
    body = request.request_body
    assert (body.receive_id, body.msg_type, body.content) == (ME, "interactive", '{"a":1}')
    assert body.uuid == send_uuid('{"a":1}') and len(body.uuid) <= 50


@pytest.mark.parametrize(
    ("result", "error"),
    [
        (response(99991663), AuthFailed),
        (response(99991400), RateLimited),
        (response(230002), PermanentError),
        (requests.exceptions.ConnectionError("refused"), TransientError),
    ],
)
def test_send_errors_are_classified(result: Any, error: type[Exception]):
    sender, _ = messenger(result)
    with pytest.raises(error):
        sender.send("open_id", ME, "{}")


def test_read_timeout_is_left_unknown():
    sender, _ = messenger(requests.exceptions.ReadTimeout("slow"))
    with pytest.raises(requests.exceptions.ReadTimeout):
        sender.send("open_id", ME, "{}")


def test_update_patches_the_clicked_message():
    sender, im = messenger(response())
    sender.update("om_9", '{"b":2}')
    (request,) = im.requests
    assert request.message_id == "om_9" and request.request_body.content == '{"b":2}'


def test_publisher_requires_receiver_and_known_id_type():
    sender, _ = messenger(response())
    with pytest.raises(ConfigError, match="receive_id"):
        FeishuPublisher({}, sender)
    with pytest.raises(ConfigError, match="receive_id_type"):
        FeishuPublisher({"receive_id": "x", "receive_id_type": "phone"}, sender)
    receipt = FeishuPublisher({"receive_id": ME}, sender).publish(Rendered("t", "{}"))
    assert receipt.external_id == "om_1"


def click_payload(operator: str = ME, value: Any = None, event_id: str = "ev1") -> Any:
    value = {"a": "save", "p": "01PAPER", "d": "01DIGEST"} if value is None else value
    return SimpleNamespace(
        header=SimpleNamespace(event_id=event_id),
        event=SimpleNamespace(
            operator=SimpleNamespace(open_id=operator),
            action=SimpleNamespace(value=value),
            context=SimpleNamespace(open_message_id="om_1"),
        ),
    )


def test_parse_click_checks_operator_and_value():
    answerers = frozenset({ME})
    click = parse_click(click_payload(), answerers)
    assert not isinstance(click, str)
    assert (click.action.action, click.action.paper_id, click.action.digest_id) == (
        "save_to_library",
        "01PAPER",
        "01DIGEST",
    )
    assert (click.actor, click.event_id, click.message_id) == (ME, "ev1", "om_1")
    assert parse_click(click_payload(operator="ou_other"), answerers) == TOAST_FORBIDDEN
    for bad in ({"a": "save"}, {"a": "nuke", "p": "1"}, {"a": "save", "p": "../x"}, "str"):
        assert parse_click(click_payload(value=bad), answerers) == TOAST_INVALID


def test_listener_queues_click_and_answers_quickly_even_if_handler_fails():
    seen: list[Any] = []
    listener = FeishuListener("id", "secret", frozenset({ME}), seen.append)
    reply = listener.handle(click_payload())
    assert seen and reply["toast"]["type"] == "info"
    assert listener.handle(click_payload(operator="ou_x"))["toast"]["content"] == TOAST_FORBIDDEN

    def broken(_: Any) -> None:
        raise RuntimeError("db locked")

    failing = FeishuListener("id", "secret", frozenset({ME}), broken)
    assert failing.handle(click_payload())["toast"]["type"] == "error"
    assert card_response(reply).toast.type == "info"


def test_answerers_default_to_the_receiver_and_groups_must_list_them():
    assert answerers_from({"receive_id": ME}) == {ME}
    assert answerers_from(
        {"receive_id": "oc_g", "receive_id_type": "chat_id", "answerers": [ME]}
    ) == {ME}
    with pytest.raises(ConfigError, match="answerers"):
        answerers_from({"receive_id": "oc_g", "receive_id_type": "chat_id"})


def test_rendered_body_is_compact_json():
    body = FeishuCardRenderer().render(view(card())).body
    assert json.loads(body)["header"]["title"]["content"] == "EEG · 2026-11-07"
