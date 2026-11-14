"""个人导出入口的授权、下载合同和安全文件边界。"""

import io
import json
import socket
import stat
from dataclasses import replace
from hashlib import sha256
from pathlib import Path, PurePosixPath
from zipfile import ZipFile

import pytest
from fastapi.testclient import TestClient
from typer.testing import CliRunner

from litgleaner import cli_library
from litgleaner.application.documents import pdf_key
from litgleaner.application.stages.discover import resolve_record
from litgleaner.cli import app as cli_app
from litgleaner.domain.export import ExportFile, export_name
from litgleaner.domain.paper import SourceRecord
from litgleaner.ports.store import FullTextState
from litgleaner.server import exports
from litgleaner.server.app import create_app
from litgleaner.server.security import MAX_BODY_BYTES, LocalSecurity
from tests.e2e.conftest import Harness

BASE = "/api/exports/mine"
PORT = 48144


@pytest.fixture
def export_ui(harness: Harness, monkeypatch: pytest.MonkeyPatch):
    paper_id = resolve_record(
        harness.ctx.store.papers,
        SourceRecord(title="Export alpha paper", authors=("Original Author",), doi="10.1234/a"),
        harness.ctx.now_iso(),
    )
    monkeypatch.setattr(cli_library, "context_or_exit", lambda _: harness.ctx)
    web = create_app(
        harness.ctx,
        LocalSecurity(PORT, launch_token="export-test"),
        None,
        context_provider=lambda: harness.ctx,
    )
    with TestClient(web, base_url=f"http://127.0.0.1:{PORT}") as client:
        client.get("/launch", params={"token": "export-test"})
        csrf = {"X-CSRF-Token": client.get("/api/session").json()["csrf_token"]}
        yield harness, client, csrf, paper_id


def unpack(content: bytes) -> dict[str, bytes]:
    with ZipFile(io.BytesIO(content)) as archive:
        assert len(archive.namelist()) == len(set(archive.namelist()))
        for info in archive.infolist():
            path = PurePosixPath(info.filename)
            assert not path.is_absolute() and ".." not in path.parts
            assert stat.S_ISREG(info.external_attr >> 16)
        return {name: archive.read(name) for name in archive.namelist()}


def check_manifest(files: dict[str, bytes]) -> dict:
    manifest = json.loads(files["manifest.json"])
    records = {item["name"]: item for item in manifest["files"]}
    assert records.keys() == files.keys() - {"manifest.json"}
    for name, data in files.items():
        if name != "manifest.json":
            assert records[name]["bytes"] == len(data)
            assert records[name]["sha256"] == sha256(data).hexdigest()
    return manifest


def saved_pdf(harness: Harness, paper_id: str) -> bytes:
    content = b"%PDF-1.4\nExisting local document\n%%EOF\n"
    digest = sha256(content).hexdigest()
    harness.ctx.blobs.put(pdf_key(digest), content)
    harness.ctx.store.papers.save_fulltext(
        FullTextState(paper_id, "available", pdf_sha256=digest), harness.ctx.now_iso()
    )
    return content


@pytest.mark.parametrize("omitted_body", [True, False])
def test_download_headers_and_manifest_are_complete_with_optional_body(export_ui, omitted_body):
    harness, client, csrf, paper_id = export_ui
    response = client.post(BASE, headers=csrf, **({} if omitted_body else {"json": {}}))
    assert response.status_code == 200, response.text
    assert response.headers["content-type"] == "application/zip"
    assert response.headers["content-disposition"] == 'attachment; filename="litgleaner-export.zip"'
    assert response.headers["cache-control"] == "no-store"
    assert response.headers["x-content-type-options"] == "nosniff"
    files = unpack(response.content)
    manifest = check_manifest(files)
    assert {
        "README.md",
        "references.bib",
        "references.csl.json",
        "NEEDS-VERIFICATION.md",
    } <= files.keys()
    assert export_name("papers", paper_id) in files
    assert manifest["format_version"] == 1 and manifest["papers"] == 1
    assert manifest["concepts"] == 0 and manifest["include_pdfs"] is False
    assert manifest["created_at"] == harness.ctx.now_iso()
    assert manifest["answer_cards"] == 0


def test_default_export_reads_no_pdf_and_works_without_models_or_budget(export_ui, monkeypatch):
    harness, client, csrf, paper_id = export_ui
    saved_pdf(harness, paper_id)
    harness.ctx = replace(harness.ctx, llm=replace(harness.ctx.llm, routes={}, daily_budget_usd=0))

    def forbidden(*_args, **_kwargs):
        raise AssertionError("export must not read blobs or open network connections")

    monkeypatch.setattr(harness.ctx.blobs, "get", forbidden)
    monkeypatch.setattr(socket, "create_connection", forbidden)
    before = harness.ctx.store.stats()
    response = client.post(BASE, json={}, headers=csrf)
    assert response.status_code == 200, response.text
    files = unpack(response.content)
    assert not any(name.startswith("pdfs/") for name in files)
    assert check_manifest(files)["warnings"] == []
    assert harness.ctx.store.stats() == before and harness.llm_calls() == 0
    assert not harness.ctx.store.jobs.list_by_status("pending")


@pytest.mark.parametrize("include_pdfs", [False, True])
def test_cli_and_api_export_the_same_snapshot_and_local_pdf(export_ui, include_pdfs, monkeypatch):
    harness, client, csrf, paper_id = export_ui
    pdf = saved_pdf(harness, paper_id)

    def forbidden(*_args, **_kwargs):
        raise AssertionError("export must not connect to external services")

    monkeypatch.setattr(socket, "create_connection", forbidden)
    before = harness.ctx.store.stats()
    response = client.post(BASE, json={"include_pdfs": include_pdfs}, headers=csrf)
    assert response.status_code == 200, response.text
    downloaded = unpack(response.content)
    out = harness.root / "new-parent" / "exported"
    args = ["export", "mine", "--out", str(out)] + (["--include-pdfs"] if include_pdfs else [])
    result = CliRunner().invoke(cli_app, args)
    assert result.exit_code == 0, (result.output, result.exception)
    written = {
        file.relative_to(out).as_posix(): file.read_bytes()
        for file in out.rglob("*")
        if file.is_file()
    }
    assert written == downloaded
    manifest = check_manifest(written)
    assert manifest["include_pdfs"] is include_pdfs and manifest["warnings"] == []
    if include_pdfs:
        assert written[export_name("pdfs", paper_id, "pdf")] == pdf
    else:
        assert not any(name.startswith("pdfs/") for name in written)
    assert harness.ctx.store.stats() == before and harness.llm_calls() == 0
    assert not harness.ctx.store.jobs.list_by_status("pending")


def test_export_requires_a_local_session(export_ui):
    harness, client, csrf, _ = export_ui
    before = harness.ctx.store.stats()
    client.cookies.clear()
    assert client.post(BASE, json={}, headers=csrf).status_code == 401
    assert harness.ctx.store.stats() == before and harness.llm_calls() == 0


@pytest.mark.parametrize("token", [None, "wrong-token"])
def test_export_requires_correct_csrf_even_with_session(export_ui, token):
    harness, client, _, _ = export_ui
    headers = {} if token is None else {"X-CSRF-Token": token}
    assert client.post(BASE, json={}, headers=headers).status_code == 403
    assert harness.llm_calls() == 0


@pytest.mark.parametrize(
    ("header", "value", "status"),
    [
        ("Host", "attacker.test", 400),
        ("Host", f"127.0.0.1:{PORT + 1}", 400),
        ("Origin", "https://attacker.test", 403),
        ("Origin", "null", 403),
        ("Origin", f"https://127.0.0.1:{PORT}", 403),
    ],
)
def test_export_rejects_host_rebinding_and_cross_origin(export_ui, header, value, status):
    harness, client, csrf, _ = export_ui
    response = client.post(BASE, json={}, headers={**csrf, header: value})
    assert response.status_code == status and harness.llm_calls() == 0


@pytest.mark.parametrize(
    "body",
    [
        {"include_pdfs": "true"},
        {"include_pdfs": "false"},
        {"include_pdfs": 1},
        {"include_pdfs": 0},
        {"include_pdfs": None},
        {"include_pdfs": []},
        {"include_pdfs": {}},
        [],
        "unexpected",
    ],
)
def test_export_rejects_wrong_body_types_and_boolean_coercion(export_ui, body):
    harness, client, csrf, _ = export_ui
    assert client.post(BASE, json=body, headers=csrf).status_code == 422
    assert harness.llm_calls() == 0 and not harness.ctx.store.jobs.list_by_status("pending")


def test_export_does_not_accept_a_browser_selected_output_path(export_ui):
    harness, client, csrf, _ = export_ui
    out = harness.root / "browser-should-not-write-here"
    response = client.post(BASE, json={"out": str(out), "include_pdfs": False}, headers=csrf)
    assert response.status_code == 422 and not out.exists()
    assert harness.llm_calls() == 0


def test_export_rejects_invalid_json_and_oversized_or_chunked_body(export_ui):
    harness, client, csrf, _ = export_ui
    headers = {**csrf, "Content-Type": "application/json"}
    assert client.post(BASE, content=b"{", headers=headers).status_code == 422
    assert (
        client.post(BASE, content=b" " * (MAX_BODY_BYTES + 1), headers=headers).status_code == 413
    )
    chunked = {**headers, "Transfer-Encoding": "chunked"}
    assert client.post(BASE, content=iter([b"{}"]), headers=chunked).status_code == 411
    assert harness.llm_calls() == 0 and not harness.ctx.store.jobs.list_by_status("pending")


def test_export_download_is_explicit_post_and_repeated_downloads_are_stable(export_ui):
    harness, client, csrf, _ = export_ui
    refused = client.get(BASE)
    assert refused.status_code in (404, 405)
    assert refused.headers["content-type"] != "application/zip"
    first = client.post(BASE, json={}, headers=csrf)
    second = client.post(BASE, json={}, headers=csrf)
    assert first.status_code == second.status_code == 200
    assert first.content == second.content
    assert harness.llm_calls() == 0


@pytest.mark.parametrize("kind", ["directory", "file", "symlink", "dangling_symlink"])
def test_cli_refuses_existing_destination_and_preserves_it(export_ui, kind):
    harness, _, _, _ = export_ui
    out = harness.root / "existing-output"
    target = harness.root / "keep-target"
    if kind == "directory":
        out.mkdir()
        (out / "keep.txt").write_bytes(b"keep-directory")
    elif kind == "file":
        out.write_bytes(b"keep-file")
    else:
        if kind == "symlink":
            target.mkdir()
            (target / "keep.txt").write_bytes(b"keep-target")
        out.symlink_to(target, target_is_directory=True)
    result = CliRunner().invoke(cli_app, ["export", "mine", "--out", str(out)])
    assert result.exit_code == 1 and "导出目录已存在" in result.output
    assert "Traceback" not in result.output
    if kind == "directory":
        assert sorted(path.name for path in out.iterdir()) == ["keep.txt"]
        assert (out / "keep.txt").read_bytes() == b"keep-directory"
    elif kind == "file":
        assert out.read_bytes() == b"keep-file"
    else:
        assert out.is_symlink() and out.readlink() == target
        if kind == "symlink":
            assert (target / "keep.txt").read_bytes() == b"keep-target"
        else:
            assert not target.exists()
    assert not list(harness.root.glob(".litgleaner-export-*"))
    assert harness.llm_calls() == 0


def test_cli_does_not_replace_a_directory_created_concurrently(export_ui, monkeypatch):
    harness, _, _, _ = export_ui
    out = harness.root / "racing-output"
    mkdir = Path.mkdir
    raced = False

    def competing_mkdir(path, *args, **kwargs):
        nonlocal raced
        if path == out and not raced:
            raced = True
            mkdir(out)
        return mkdir(path, *args, **kwargs)

    monkeypatch.setattr(Path, "mkdir", competing_mkdir)
    result = CliRunner().invoke(cli_app, ["export", "mine", "--out", str(out)])
    assert raced and result.exit_code == 1
    assert "导出目录已存在" in result.output
    assert out.is_dir() and not list(out.iterdir())
    assert harness.llm_calls() == 0


def test_cli_removes_partial_staging_if_content_generation_fails(export_ui, monkeypatch):
    harness, _, _, _ = export_ui
    parent = harness.root / "destination"
    parent.mkdir()
    sentinel = parent / "keep.txt"
    sentinel.write_bytes(b"unrelated data")

    def broken(*_args, **_kwargs):
        yield ExportFile("README.md", b"partial data")
        raise OSError("simulated generator failure with private path")

    monkeypatch.setattr(cli_library, "mine", broken)
    out = parent / "exported"
    result = CliRunner().invoke(cli_app, ["export", "mine", "--out", str(out)])
    assert result.exit_code == 1 and not out.exists()
    assert "导出写入失败" in result.output and "Traceback" not in result.output
    assert sorted(path.name for path in parent.iterdir()) == ["keep.txt"]
    assert sentinel.read_bytes() == b"unrelated data"


def test_api_reports_export_failure_without_serving_partial_zip_or_private_error(
    export_ui, monkeypatch
):
    harness, client, csrf, _ = export_ui

    def broken(*_args, **_kwargs):
        yield ExportFile("README.md", b"partial data")
        raise OSError("simulated generator failure with private path")

    monkeypatch.setattr(exports, "mine", broken)
    before = harness.ctx.store.stats()
    response = client.post(BASE, json={}, headers=csrf)
    assert response.status_code == 422 and response.headers["content-type"] == "application/json"
    assert response.json()["code"] == "invalid_request"
    assert "private" not in response.text and "simulated" not in response.text
    assert harness.ctx.store.stats() == before and harness.llm_calls() == 0
