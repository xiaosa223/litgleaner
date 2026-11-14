"""上传任务书推断研究画像（ADR 0012）：HTTP 验收，模型用假适配器，不写课题文件。"""

from urllib.parse import quote

import pytest
from fastapi.testclient import TestClient

from litgleaner.infra.config import load_config
from litgleaner.server.app import create_app
from litgleaner.server.security import DOCUMENT_PATH, MAX_DOCUMENT_BYTES, LocalSecurity
from litgleaner.service import ModelRuntime
from tests.e2e.conftest import Harness
from tests.e2e.test_model_settings import MemorySecrets
from tests.helpers.pdf import make_pdf


def _with_profile_route(data):
    data["tasks"]["profile"] = {"provider": "main", "model": "strong", "prompt": "profile"}


def _client(harness: Harness) -> TestClient:
    runtime = ModelRuntime(load_config(harness.root / "config"), harness.ctx, MemorySecrets())
    app = create_app(
        harness.ctx,
        LocalSecurity(48153, launch_token="profile-test"),
        None,
        context_provider=runtime.snapshot,
        model_settings=runtime,
        topic_settings=runtime,
    )
    client = TestClient(app, base_url="http://127.0.0.1:48153")
    client.get("/launch", params={"token": "profile-test"})
    client.headers["X-CSRF-Token"] = client.get("/api/session").json()["csrf_token"]
    return client


def _upload(client: TestClient, name: str, content: bytes):
    return client.post(
        DOCUMENT_PATH,
        content=content,
        headers={"Content-Type": "application/octet-stream", "X-File-Name": quote(name)},
    )


@pytest.fixture
def routed(harness: Harness) -> Harness:
    harness.edit_yaml("llm.yaml", _with_profile_route)
    harness.rebuild()
    return harness


def test_pdf_brief_fills_profile_fields_without_creating_a_topic(routed: Harness):
    client = _client(routed)
    topics_before = sorted((routed.root / "config" / "topics").iterdir())
    pdf = make_pdf([["Project brief: EEG foundation models for few-shot transfer"]])

    response = _upload(client, "课题任务书.pdf", pdf)

    assert response.status_code == 200, response.text
    assert response.json() == {
        "name": "演示课题",
        "background": "演示背景",
        "content": "演示内容",
        "goals": ["演示目标一", "演示目标二"],
    }
    assert sorted((routed.root / "config" / "topics").iterdir()) == topics_before
    (call,) = [c for c in routed.llm.calls if c.task == "profile"]
    assert "EEG foundation models" in call.user
    assert "<untrusted" in call.user


def test_scanned_or_unsupported_documents_get_fixed_codes(routed: Harness):
    client = _client(routed)
    scanned = _upload(client, "scan.pdf", make_pdf([[]]))
    assert scanned.status_code == 422 and "document_no_text" in scanned.text
    image = _upload(client, "photo.png", b"\x89PNG\r\n\x1a\n" + b"\x00" * 32)
    assert image.status_code == 422 and "document_type_unsupported" in image.text
    assert not [c for c in routed.llm.calls if c.task == "profile"]


def test_oversized_document_is_refused_before_reading(routed: Harness):
    client = _client(routed)
    response = client.post(
        DOCUMENT_PATH,
        content=b"x",
        headers={"Content-Length": str(MAX_DOCUMENT_BYTES + 1)},
    )
    assert response.status_code == 413


def test_without_profile_model_asks_for_configuration(harness: Harness):
    response = _upload(_client(harness), "brief.txt", "研究背景".encode())
    assert response.status_code == 409
    assert response.json()["code"] == "model_configuration_required"
    assert response.json()["task"] == "profile"
