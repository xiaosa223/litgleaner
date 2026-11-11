"""运行锁的 Windows 协议与导入边界，仅使用内存句柄。"""

import builtins
import errno
import os
from io import BytesIO
from pathlib import Path
from types import SimpleNamespace

import pytest
import typer

import litgleaner.cli_shared as shared


class Handle(BytesIO):
    def fileno(self):
        return 42


def windows(monkeypatch, error=None):
    calls = []

    def locking(fd, mode, size):
        calls.append((fd, mode, size))
        if error is not None:
            raise error

    monkeypatch.setattr(shared, "sys", SimpleNamespace(platform="win32"), raising=False)
    monkeypatch.setattr(
        shared, "msvcrt", SimpleNamespace(LK_NBLCK=2, locking=locking), raising=False
    )
    return calls


def test_windows_module_import_does_not_require_fcntl():
    original = builtins.__import__
    imports = []

    def importing(name, *args, **kwargs):
        imports.append(name)
        if name == "fcntl":
            raise ModuleNotFoundError("No module named 'fcntl'")
        if name == "sys":
            return SimpleNamespace(platform="win32")
        if name == "msvcrt":
            return SimpleNamespace(LK_NBLCK=2, locking=lambda *_: None)
        return original(name, *args, **kwargs)

    namespace = {**shared.__dict__, "__builtins__": {**vars(builtins), "__import__": importing}}
    source = Path(shared.__file__).read_text(encoding="utf-8")
    exec(compile(source, shared.__file__, "exec"), namespace)
    assert "fcntl" not in imports and "msvcrt" in imports


@pytest.mark.parametrize("initial", [b"", b"existing lock metadata"])
def test_windows_locks_first_byte_without_truncating_existing_content(monkeypatch, initial):
    calls = windows(monkeypatch)
    with Handle(initial) as handle:
        handle.seek(0, 2)
        assert shared._try_lock(handle)
        assert handle.tell() == 0
        assert handle.getvalue() == (initial or b"\0")
    assert calls == [(42, 2, 1)] and handle.closed


@pytest.mark.parametrize("code", [errno.EACCES, errno.EAGAIN, errno.EDEADLK])
def test_windows_known_contention_returns_false(monkeypatch, code):
    calls = windows(monkeypatch, OSError(code, "another owner"))
    with Handle(b"0") as handle:
        assert shared._try_lock(handle) is False
    assert calls == [(42, 2, 1)]


@pytest.mark.parametrize("code", [errno.EBADF, errno.EIO, errno.EINVAL, errno.ENOSPC])
def test_windows_other_lock_errors_propagate(monkeypatch, code):
    windows(monkeypatch, OSError(code, "broken locking"))
    with Handle(b"0") as handle, pytest.raises(OSError) as raised:
        shared._try_lock(handle)
    assert raised.value.errno == code


def test_windows_write_failure_is_not_mistaken_for_contention(monkeypatch):
    calls = windows(monkeypatch)

    class Unwritable(Handle):
        def write(self, _data):
            raise PermissionError(errno.EACCES, "write denied")

    with Unwritable() as handle, pytest.raises(PermissionError, match="write denied"):
        shared._try_lock(handle)
    assert calls == []


def test_windows_flush_failure_is_not_mistaken_for_contention(monkeypatch):
    calls = windows(monkeypatch)

    class Unflushable(Handle):
        def flush(self):
            raise OSError(errno.EIO, "flush failed")

    handle = Unflushable()
    with pytest.raises(OSError, match="flush failed"):
        shared._try_lock(handle)
    assert calls == []
    handle.close()


@pytest.mark.parametrize("owner", [shared.run_lock, shared.try_run_lock])
def test_windows_public_context_closes_handle_after_body_error(tmp_path, monkeypatch, owner):
    calls = windows(monkeypatch)
    settings = SimpleNamespace(
        app=SimpleNamespace(database="data.db"), path=lambda _: tmp_path / "data.db"
    )
    with pytest.raises(RuntimeError, match="body failed"), owner(settings):
        raise RuntimeError("body failed")
    assert len(calls) == 1
    with pytest.raises(OSError) as raised:
        os.fstat(calls[0][0])
    assert raised.value.errno == errno.EBADF
    assert (tmp_path / "data.lock").read_bytes() == b"\0"


@pytest.mark.parametrize("owner", [shared.run_lock, shared.try_run_lock])
def test_windows_public_context_closes_handle_after_lock_error(tmp_path, monkeypatch, owner):
    calls = windows(monkeypatch, OSError(errno.EIO, "locking broken"))
    settings = SimpleNamespace(
        app=SimpleNamespace(database="data.db"), path=lambda _: tmp_path / "data.db"
    )
    with pytest.raises(OSError, match="locking broken"), owner(settings):
        pytest.fail("unknown I/O errors cannot report lock success or contention")
    with pytest.raises(OSError) as raised:
        os.fstat(calls[0][0])
    assert raised.value.errno == errno.EBADF


def test_windows_public_contention_has_original_exit_code_or_false(tmp_path, monkeypatch, capsys):
    calls = windows(monkeypatch, PermissionError(errno.EACCES, "another owner"))
    settings = SimpleNamespace(
        app=SimpleNamespace(database="data.db"), path=lambda _: tmp_path / "data.db"
    )
    with pytest.raises(typer.Exit) as raised, shared.run_lock(settings):
        pytest.fail("run_lock must exit instead of entering the locked body")
    assert raised.value.exit_code == 3 and "已有流程正在运行" in capsys.readouterr().err
    with shared.try_run_lock(settings) as acquired:
        assert acquired is False
    assert len(calls) == 2
    for fd, _, _ in calls:
        with pytest.raises(OSError):
            os.fstat(fd)
