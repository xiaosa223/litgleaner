"""MinerU 云端解析：用模拟 HTTP 服务验证调用流程、错误分类、断点续查和密钥隔离。"""

import io
import json
import zipfile
from pathlib import Path

import httpx
import pytest

from litfactory.adapters.parsers.mineru_cloud import MinerUCloudParser
from litfactory.domain.errors import (
    AuthFailed,
    ConfigError,
    Deferred,
    PermanentError,
    RateLimited,
    SchemaChanged,
    TransientError,
)

TOKEN = "tok-SECRET-1234567890"
UPLOAD_URL = "https://oss.example.com/upload/abc.pdf?Signature=SIGNED-SECRET&Expires=1"
ZIP_URL = "https://cdn.example.com/result.zip"
SHA = "ab" * 32
PDF = b"%PDF-1.7\nhello"
FIXTURE = Path(__file__).parents[1] / "fixtures" / "mineru" / "arxiv-1907.10485v1.content_list.json"


def result_zip(members: dict[str, bytes]) -> bytes:
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as archive:
        for name, data in members.items():
            archive.writestr(name, data)
    return buffer.getvalue()


CONTENT = FIXTURE.read_bytes()
GOOD_ZIP = result_zip(
    {
        "full.md": b"# x",
        "layout.json": b"{}",
        "uuid_content_list.json": CONTENT,
        "uuid_content_list_v2.json": b"[[]]",
    }
)


class FakeMinerU:
    """模拟 MinerU：记录收到的请求，按脚本返回状态。"""

    def __init__(self, states: list[dict] | None = None, zip_bytes: bytes = GOOD_ZIP):
        self.states = states or [{"state": "pending"}, {"state": "running"}, {"state": "done"}]
        self.zip_bytes = zip_bytes
        self.requests: list[httpx.Request] = []
        self.create_response: dict = {
            "code": 0,
            "msg": "ok",
            "data": {"batch_id": "batch-1", "file_urls": [UPLOAD_URL]},
        }
        self.poll_override: dict | None = None
        self.upload_status = 200

    def handler(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        url = str(request.url)
        if request.method == "POST" and url.endswith("/api/v4/file-urls/batch"):
            return httpx.Response(200, json=self.create_response)
        if request.method == "PUT" and url == UPLOAD_URL:
            return httpx.Response(self.upload_status)
        if "/api/v4/extract-results/batch/" in url:
            if self.poll_override:
                return httpx.Response(200, json=self.poll_override)
            state = self.states.pop(0) if len(self.states) > 1 else self.states[0]
            entry = {"file_name": "paper.pdf", "err_msg": "", **state}
            if state["state"] == "done":
                entry["full_zip_url"] = ZIP_URL
            body = {"code": 0, "msg": "ok", "data": {"extract_result": [entry]}}
            return httpx.Response(200, json=body)
        if url == ZIP_URL:
            return httpx.Response(200, content=self.zip_bytes)
        return httpx.Response(404)

    def count(self, method: str, fragment: str = "") -> int:
        return sum(1 for r in self.requests if r.method == method and fragment in str(r.url))


class Clock:
    def __init__(self) -> None:
        self.now = 0.0

    def monotonic(self) -> float:
        return self.now

    def sleep(self, seconds: float) -> None:
        self.now += seconds


def make_parser(
    server: FakeMinerU, tmp_path: Path, clock: Clock | None = None, wall: float = 0.0, **options
):
    clock = clock or Clock()
    transport = httpx.MockTransport(server.handler)
    return MinerUCloudParser(
        token=TOKEN,
        options={"state_dir": str(tmp_path / "state"), "poll_interval_seconds": 5, **options},
        transport=transport,
        sleep=clock.sleep,
        monotonic=clock.monotonic,
        wall_clock=lambda: wall,
    )


def test_happy_path_returns_document_and_raw_content_list(tmp_path):
    server = FakeMinerU()
    result = make_parser(server, tmp_path).parse(PDF, SHA)
    assert result.document.parser_id == "mineru_cloud@vlm"
    assert result.document.source_pdf_sha256 == SHA
    assert len([b for b in result.document.blocks if b.kind == "equation"]) == 11
    assert result.raw == CONTENT
    assert server.count("POST") == 1
    assert server.count("PUT") == 1


def test_request_body_and_credential_isolation(tmp_path):
    server = FakeMinerU()
    make_parser(server, tmp_path, language="en").parse(PDF, SHA)
    create, upload = server.requests[0], server.requests[1]
    body = json.loads(create.content)
    assert body["model_version"] == "vlm"
    assert body["language"] == "en"
    assert body["files"] == [{"name": f"{SHA[:32]}.pdf", "data_id": SHA[:32]}]
    assert create.headers["Authorization"] == f"Bearer {TOKEN}"
    # 签名上传链接和结果下载都不能带 token；上传不能带 Content-Type，否则签名失效
    for request in server.requests[1:]:
        if "/api/" not in str(request.url):
            assert "Authorization" not in request.headers
    assert "content-type" not in {key.lower() for key in upload.headers}
    assert upload.content == PDF


@pytest.mark.parametrize(
    ("code", "error"),
    [
        ("A0202", AuthFailed),
        ("A0211", AuthFailed),
        (-60005, PermanentError),
        (-60006, PermanentError),
        (-60018, RateLimited),
        (-60009, TransientError),
        (-500, SchemaChanged),
    ],
)
def test_api_error_codes_are_classified(tmp_path, code, error):
    server = FakeMinerU()
    server.create_response = {"code": code, "msg": f"failure {code}"}
    with pytest.raises(error):
        make_parser(server, tmp_path).parse(PDF, SHA)


@pytest.mark.parametrize(
    ("status", "error"),
    [(401, AuthFailed), (403, AuthFailed), (429, RateLimited), (502, TransientError)],
)
def test_http_status_is_classified(tmp_path, status, error):
    server = FakeMinerU()
    server.handler = lambda request: httpx.Response(status)  # type: ignore[method-assign]
    with pytest.raises(error):
        make_parser(server, tmp_path).parse(PDF, SHA)


def test_daily_quota_is_deferred_not_failed(tmp_path):
    server = FakeMinerU()
    server.create_response = {"code": -60018, "msg": "quota"}
    with pytest.raises(Deferred) as raised:
        make_parser(server, tmp_path).parse(PDF, SHA)
    assert raised.value.retry_after_seconds >= 3600


def test_failed_state_is_permanent_with_reason(tmp_path):
    server = FakeMinerU(states=[{"state": "failed", "err_msg": "文件损坏"}])
    with pytest.raises(PermanentError, match="文件损坏"):
        make_parser(server, tmp_path).parse(PDF, SHA)


def test_timeout_defers_and_next_call_resumes_without_resubmitting(tmp_path):
    clock = Clock()
    server = FakeMinerU(states=[{"state": "running"}])
    parser = make_parser(server, tmp_path, clock, timeout_seconds=30)
    with pytest.raises(Deferred):
        parser.parse(PDF, SHA)
    assert server.count("POST") == 1

    server.states = [{"state": "done"}]
    result = make_parser(server, tmp_path, clock, timeout_seconds=30).parse(PDF, SHA)
    assert result.raw == CONTENT
    assert server.count("POST") == 1, "续查时不能重新提交，否则会重复消耗额度"
    assert server.count("PUT") == 1
    assert not list((tmp_path / "state").glob("*.json")), "完成后要清除续查记录"


def test_lost_batch_is_resubmitted(tmp_path):
    clock = Clock()
    server = FakeMinerU(states=[{"state": "running"}])
    with pytest.raises(Deferred):
        make_parser(server, tmp_path, clock, timeout_seconds=10).parse(PDF, SHA)
    server.poll_override = {"code": -60012, "msg": "task not found"}
    with pytest.raises(TransientError):
        make_parser(server, tmp_path, clock, timeout_seconds=10).parse(PDF, SHA)
    server.poll_override = None
    server.states = [{"state": "done"}]
    make_parser(server, tmp_path, clock, timeout_seconds=10).parse(PDF, SHA)
    assert server.count("POST") == 2


def test_failed_upload_does_not_leave_resume_state(tmp_path):
    server = FakeMinerU()
    server.upload_status = 500
    with pytest.raises(TransientError):
        make_parser(server, tmp_path).parse(PDF, SHA)
    assert not list((tmp_path / "state").glob("*.json"))


def test_errors_never_contain_token_or_signed_url(tmp_path):
    server = FakeMinerU()
    server.create_response = {"code": -60009, "msg": f"echo {TOKEN} {UPLOAD_URL}"}
    with pytest.raises(TransientError) as raised:
        make_parser(server, tmp_path).parse(PDF, SHA)
    text = str(raised.value)
    assert TOKEN not in text
    assert "SIGNED-SECRET" not in text


def test_result_without_content_list_is_schema_changed(tmp_path):
    server = FakeMinerU(zip_bytes=result_zip({"full.md": b"#"}))
    with pytest.raises(SchemaChanged, match="content_list"):
        make_parser(server, tmp_path).parse(PDF, SHA)


def test_oversized_archive_member_is_rejected(tmp_path):
    big = result_zip({"x_content_list.json": b"[" + b" " * 2_000_000 + b"]"})
    server = FakeMinerU(zip_bytes=big)
    with pytest.raises(PermanentError, match="过大"):
        make_parser(server, tmp_path, max_content_list_mb=1).parse(PDF, SHA)


def test_non_https_result_url_is_refused(tmp_path):
    server = FakeMinerU()
    original = server.handler

    def handler(request: httpx.Request) -> httpx.Response:
        response = original(request)
        if "/extract-results/" in str(request.url):
            body = response.json()
            body["data"]["extract_result"][0]["full_zip_url"] = "http://cdn.example.com/x.zip"
            return httpx.Response(200, json=body)
        return response

    server.handler = handler  # type: ignore[method-assign]
    with pytest.raises(SchemaChanged, match="https"):
        make_parser(server, tmp_path).parse(PDF, SHA)


def test_file_over_size_limit_is_rejected_before_upload(tmp_path):
    server = FakeMinerU()
    with pytest.raises(PermanentError, match="大小"):
        make_parser(server, tmp_path, max_file_mb=1).parse(b"%PDF" + b"0" * 2_000_000, SHA)
    assert server.requests == []


def test_missing_token_or_bad_options_fail_at_startup(tmp_path):
    with pytest.raises(ConfigError, match="MINERU"):
        MinerUCloudParser(token="", options={"state_dir": str(tmp_path)})
    with pytest.raises(ConfigError, match="model_version"):
        MinerUCloudParser(token=TOKEN, options={"state_dir": str(tmp_path), "model_version": "x"})


def test_old_resume_state_is_kept_not_resubmitted(tmp_path):
    """续查记录不按时间过期：只有服务端明确说任务不存在才重新提交。"""
    clock = Clock()
    server = FakeMinerU(states=[{"state": "running"}])
    with pytest.raises(Deferred):
        make_parser(server, tmp_path, clock, timeout_seconds=10).parse(PDF, SHA)
    later = make_parser(server, tmp_path, clock, wall=30 * 24 * 3600.0, timeout_seconds=10)
    server.states = [{"state": "done"}]
    later.parse(PDF, SHA)
    assert server.count("POST") == 1


@pytest.mark.parametrize("content", ["[]", '{"batch_id": 1}', '{"submitted_at": "x"}', "not json"])
def test_corrupt_resume_state_is_discarded(tmp_path, content):
    clock = Clock()
    server = FakeMinerU(states=[{"state": "running"}])
    with pytest.raises(Deferred):
        make_parser(server, tmp_path, clock, timeout_seconds=10).parse(PDF, SHA)
    [state_file] = list((tmp_path / "state").glob("*.json"))
    state_file.write_text(content, encoding="utf-8")
    server.states = [{"state": "done"}]
    make_parser(server, tmp_path, clock, timeout_seconds=10).parse(PDF, SHA)
    assert server.count("POST") == 2, "损坏的续查记录应当丢弃并重新提交，而不是让任务反复失败"


def test_resume_state_is_per_model_version(tmp_path):
    clock = Clock()
    server = FakeMinerU(states=[{"state": "running"}])
    with pytest.raises(Deferred):
        make_parser(server, tmp_path, clock, timeout_seconds=10).parse(PDF, SHA)
    server.states = [{"state": "done"}]
    make_parser(server, tmp_path, clock, model_version="pipeline").parse(PDF, SHA)
    assert server.count("POST") == 2, "换了模型不能复用旧模型的任务"


def test_failed_state_message_is_scrubbed(tmp_path):
    server = FakeMinerU(states=[{"state": "failed", "err_msg": f"bad {TOKEN} {UPLOAD_URL}"}])
    with pytest.raises(PermanentError) as raised:
        make_parser(server, tmp_path).parse(PDF, SHA)
    assert TOKEN not in str(raised.value)
    assert "SIGNED-SECRET" not in str(raised.value)


def test_ambiguous_archive_with_two_content_lists_is_rejected(tmp_path):
    archive = result_zip({"a_content_list.json": b"[]", "b_content_list.json": b"[]"})
    with pytest.raises(SchemaChanged, match="多个"):
        make_parser(FakeMinerU(zip_bytes=archive), tmp_path).parse(PDF, SHA)
