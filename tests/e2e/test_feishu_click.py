"""推送一张飞书卡片，点“加入 Zotero”，论文写进文献库，卡片更新成“已加入”。

发送用假渠道（只换渲染器为飞书卡片），点击直接构造监听器交给工作线程的 CardClick。
"""

import json
from dataclasses import replace
from typing import Any

from sqlalchemy import select

from litgleaner.cli_library import _handle_click
from litgleaner.cli_shared import try_run_lock
from litgleaner.domain.library import decode_action
from litgleaner.infra.config import load_config
from litgleaner.infra.db.schema import feedback
from litgleaner.ports.inbound import CardClick
from litgleaner.ports.store import DigestItem
from tests.e2e.conftest import Harness, paper


def setup(harness: Harness) -> dict[str, Any]:
    harness.edit_yaml("app.yaml", lambda d: d.update(library={"adapter": "fake_library"}))
    harness.edit_yaml(
        "channels.yaml",
        lambda d: d["channels"]["me"].update(
            renderer="feishu_card", options={"receive_id": "ou_me"}
        ),
    )
    harness.rebuild()
    harness.write_papers([paper("Top Paper", 5), paper("Good Paper", 3)])
    assert harness.run().ok
    return json.loads(harness.publisher().sent[0].body)


def save_button_values(card: dict[str, Any]) -> list[dict[str, str]]:
    return [
        column["elements"][0]["behaviors"][0]["value"]
        for element in card["body"]["elements"]
        if element["tag"] == "column_set"
        for column in element["columns"]
        if column["elements"][0]["behaviors"][0].get("value", {}).get("a") == "save"
    ]


def test_click_saves_paper_and_updates_card(harness: Harness):
    card = setup(harness)
    first, second = save_button_values(card)
    action = decode_action(first)
    assert action is not None
    updates: list[tuple[str, str]] = []
    click = CardClick(action, "ou_me", "ev1", "om_1")
    loaded = load_config(harness.root / "config")

    _handle_click(harness.ctx, loaded, "me", click, lambda mid, body: updates.append((mid, body)))

    (saved,) = harness.ctx.library.saved  # type: ignore[union-attr]
    assert saved.key == action.paper_id
    ((message_id, body),) = updates
    assert message_id == "om_1"
    assert "已加入 Zotero" in body
    assert len(save_button_values(json.loads(body))) == 1  # 另一篇的按钮还在
    assert save_button_values(json.loads(body))[0] == second

    # 同一次点击重复送达：不会重复写入。
    _handle_click(harness.ctx, loaded, "me", click, lambda *_: None)
    assert len(harness.ctx.library.saved) == 1  # type: ignore[union-attr]


def test_standalone_click_keeps_business_writes_behind_the_shared_lock(harness: Harness):
    action = decode_action(save_button_values(setup(harness))[0])
    assert action is not None
    click = CardClick(action, "ou_me", "ev-lock", "om_1")
    loaded = load_config(harness.root / "config")
    updates = []

    def update(message_id, body):
        with try_run_lock(loaded) as acquired:
            assert acquired
        updates.append((message_id, body))

    with try_run_lock(loaded) as acquired:
        assert acquired
        assert not _handle_click(harness.ctx, loaded, "me", click, update)
        assert harness.ctx.store._db.fetch(select(feedback)) == []
        assert harness.ctx.library.saved == []
    assert _handle_click(harness.ctx, loaded, "me", click, update)
    assert len(updates) == len(harness.ctx.library.saved) == 1


def test_standalone_listener_cannot_record_another_channels_digest(harness: Harness):
    action = decode_action(save_button_values(setup(harness))[0])
    assert action is not None
    original = harness.ctx.store.outputs.get_digest(action.digest_id)
    assert original is not None
    card = harness.ctx.store.outputs.digest_cards(original.id)[0]
    harness.ctx.store.outputs.create_digest(
        replace(original, id="other-digest", channel="other", route_id="other-route"),
        (DigestItem(card["paper_id"], False, card),),
        harness.ctx.now_iso(),
    )
    click = CardClick(replace(action, digest_id="other-digest"), "ou_me", "ev-other", "om_1")
    updates = []
    before = harness.llm_calls()
    assert _handle_click(
        harness.ctx,
        load_config(harness.root / "config"),
        "me",
        click,
        lambda *args: updates.append(args),
    )
    assert harness.ctx.store._db.fetch(select(feedback)) == []
    assert harness.ctx.library.saved == [] and updates == []
    assert harness.llm_calls() == before
