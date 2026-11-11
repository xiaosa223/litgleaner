"""监听组装复用已声明凭据；未启用时不接触 SDK、网络或系统钥匙串。"""

from types import SimpleNamespace

import pytest

from litgleaner import bootstrap
from litgleaner.adapters.feishu import api
from litgleaner.domain.errors import ConfigError
from litgleaner.infra.config import load_config
from tests.e2e.conftest import Harness

APP_ID = "FEISHU_APP_ID"
APP_SECRET = "FEISHU_APP_SECRET"
DECLARED = [APP_ID, APP_SECRET]


def channel(receiver="ou_alice", *, enabled=True, credentials=None, **options):
    return {
        "adapter": "feishu",
        "renderer": "feishu_card",
        "visibility": "private",
        "enabled": enabled,
        "credentials_env": DECLARED if credentials is None else credentials,
        "options": {"receive_id": receiver, **options},
    }


def configured(harness: Harness, **channels):
    harness.edit_yaml("channels.yaml", lambda data: data["channels"].update(channels))
    return load_config(harness.root / "config")


def click(actor):
    return SimpleNamespace(
        header=SimpleNamespace(event_id=f"event-{actor}"),
        event=SimpleNamespace(
            operator=SimpleNamespace(open_id=actor),
            action=SimpleNamespace(value={"a": "save", "p": "PAPER", "d": "DIGEST"}),
            context=SimpleNamespace(open_message_id="MESSAGE"),
        ),
    )


@pytest.fixture
def isolated(monkeypatch):
    values = {APP_ID: "test-app-id", APP_SECRET: "test-app-secret"}
    reads, messengers, updates = [], [], []

    class Secrets:
        def get(self, name):
            reads.append(name)
            return values.get(name)

    class Messenger:
        def __init__(self, app_id, secret):
            messengers.append((self, app_id, secret))

        def update(self, message_id, body):
            updates.append((message_id, body))

    def forbidden(*_args, **_kwargs):
        pytest.fail("组装监听不能加载 SDK、访问真实网络或系统钥匙串")

    monkeypatch.setattr(bootstrap, "SystemSecretStore", Secrets)
    monkeypatch.setattr(bootstrap, "FeishuMessenger", Messenger)
    monkeypatch.setattr(api, "sdk", forbidden)
    return SimpleNamespace(values=values, reads=reads, messengers=messengers, updates=updates)


@pytest.mark.parametrize("disabled", [False, True])
def test_no_enabled_inbound_channel_never_reads_credentials_or_loads_sdk(
    harness: Harness, isolated, disabled
):
    config = (
        configured(harness, dormant=channel(enabled=False)) if disabled else configured(harness)
    )
    with pytest.raises(ConfigError, match="inbound_disabled"):
        bootstrap.build_feishu_listener(config, None, lambda _: None)
    assert isolated.reads == isolated.messengers == []


def test_multiple_channels_share_connection_and_keep_per_channel_answerers(
    harness: Harness, isolated
):
    config = configured(
        harness,
        alice=channel(),
        bob=channel("ou_bob"),
        dormant=channel(enabled=False, credentials=[]),
    )
    seen = []
    listener, update = bootstrap.build_feishu_listener(config, None, seen.append)
    assert listener.channels == {"alice": frozenset({"ou_alice"}), "bob": frozenset({"ou_bob"})}
    assert len(isolated.messengers) == 1
    assert isolated.reads == DECLARED
    assert listener.handle(click("ou_alice"))["toast"]["type"] == "info"
    assert listener.handle(click("ou_bob"))["toast"]["type"] == "info"
    assert listener.handle(click("ou_outsider"))["toast"]["type"] == "warning"
    assert [event.actor for event in seen] == ["ou_alice", "ou_bob"]
    update("MESSAGE", "card-body")
    assert isolated.updates == [("MESSAGE", "card-body")]


def test_named_channel_ignores_other_channels_and_their_missing_declarations(
    harness: Harness, isolated
):
    config = configured(harness, alice=channel(), bob=channel("ou_bob", credentials=[]))
    seen = []
    listener, _ = bootstrap.build_feishu_listener(config, "alice", seen.append)
    assert listener.channels == {"alice": frozenset({"ou_alice"})}
    assert listener.handle(click("ou_bob"))["toast"]["type"] == "warning"
    assert seen == [] and len(isolated.messengers) == 1
    assert isolated.reads == DECLARED


@pytest.mark.parametrize("name", ["missing", "me", "dormant"])
def test_unknown_non_inbound_or_disabled_named_channel_is_rejected_before_secrets(
    harness: Harness, isolated, name
):
    config = configured(harness, alice=channel(), dormant=channel(enabled=False))
    with pytest.raises(ConfigError, match="inbound_disabled"):
        bootstrap.build_feishu_listener(config, name, lambda _: None)
    assert isolated.reads == isolated.messengers == []


@pytest.mark.parametrize("credentials", [[], [APP_ID], [APP_SECRET]])
def test_missing_credential_declaration_never_reads_an_undeclared_secret(
    harness: Harness, isolated, credentials
):
    config = configured(harness, alice=channel(credentials=credentials))
    with pytest.raises(ConfigError, match="credentials_env"):
        bootstrap.build_feishu_listener(config, None, lambda _: None)
    assert set(isolated.reads) <= set(credentials)
    assert isolated.messengers == []


def test_shared_connection_requires_credential_declarations_from_every_selected_channel(
    harness: Harness, isolated
):
    config = configured(harness, alice=channel(), bob=channel("ou_bob", credentials=[APP_ID]))
    with pytest.raises(ConfigError, match=APP_SECRET):
        bootstrap.build_feishu_listener(config, None, lambda _: None)
    assert APP_SECRET not in isolated.reads
    assert isolated.messengers == []


@pytest.mark.parametrize("missing", DECLARED)
def test_missing_credential_value_fails_before_creating_message_client(
    harness: Harness, isolated, missing
):
    isolated.values.pop(missing)
    config = configured(harness, alice=channel())
    with pytest.raises(ConfigError, match=r"FEISHU_APP_ID.*FEISHU_APP_SECRET") as error:
        bootstrap.build_feishu_listener(config, None, lambda _: None)
    assert isolated.reads == DECLARED and isolated.messengers == []
    assert all(value not in str(error.value) for value in isolated.values.values())


def test_group_channel_without_answerers_fails_without_starting_connection(
    harness: Harness, isolated
):
    config = configured(harness, group=channel("oc_group", receive_id_type="chat_id"))
    with pytest.raises(ConfigError, match="answerers"):
        bootstrap.build_feishu_listener(config, None, lambda _: None)
    assert set(isolated.reads) <= set(DECLARED)
    assert isolated.messengers == []


def test_group_channel_uses_its_explicit_subscription_whitelist(harness: Harness, isolated):
    config = configured(
        harness,
        group=channel("oc_group", receive_id_type="chat_id", answerers=["ou_alice", "ou_bob"]),
    )
    seen = []
    listener, _ = bootstrap.build_feishu_listener(config, None, seen.append)
    assert listener.channels == {"group": frozenset({"ou_alice", "ou_bob"})}
    assert listener.handle(click("oc_group"))["toast"]["type"] == "warning"
    assert listener.handle(click("ou_bob"))["toast"]["type"] == "info"
    assert [event.actor for event in seen] == ["ou_bob"]
