"""共享运行锁的真实文件、异常释放和独立进程互斥。"""

import multiprocessing
import subprocess
import sys
import textwrap
from pathlib import Path
from types import SimpleNamespace

import pytest
import typer

from litgleaner.cli_shared import run_lock, try_run_lock


def config(directory: Path, name="litgleaner.db"):
    return SimpleNamespace(
        app=SimpleNamespace(database=name), path=lambda relative: directory / relative
    )


def child_attempt(directory, queue):
    with try_run_lock(config(Path(directory))) as acquired:
        queue.put(acquired)


def test_both_public_lock_apis_share_one_nonblocking_lock(tmp_path, capsys):
    settings = config(tmp_path / "中文 目录")
    with run_lock(settings):
        with try_run_lock(settings) as acquired:
            assert acquired is False
        with pytest.raises(typer.Exit) as raised, run_lock(settings):
            pytest.fail("run_lock must fail while the first context owns the file")
        assert raised.value.exit_code == 3
    assert "已有流程正在运行" in capsys.readouterr().err
    with try_run_lock(settings) as acquired:
        assert acquired


def test_try_lock_owner_also_excludes_run_lock(tmp_path):
    settings = config(tmp_path)
    with try_run_lock(settings) as acquired:
        assert acquired
        with pytest.raises(typer.Exit), run_lock(settings):
            pytest.fail("run_lock must use the same file as try_run_lock")


@pytest.mark.parametrize("owner", [run_lock, try_run_lock])
def test_context_body_exception_releases_lock_and_keeps_inode(tmp_path, owner):
    settings = config(tmp_path)
    path = tmp_path / "litgleaner.lock"
    path.write_bytes(b"existing lock metadata")
    inode = path.stat().st_ino
    with pytest.raises(RuntimeError, match="body failed"), owner(settings):
        raise RuntimeError("body failed")
    assert path.read_bytes() == b"existing lock metadata"
    assert path.stat().st_ino == inode
    with run_lock(settings):
        assert path.stat().st_ino == inode


def test_different_databases_have_independent_locks(tmp_path):
    with (
        run_lock(config(tmp_path, "first.db")),
        try_run_lock(config(tmp_path, "second.db")) as acquired,
    ):
        assert acquired


def test_true_spawn_process_observes_lock_and_release(tmp_path):
    context = multiprocessing.get_context("spawn")
    settings = config(tmp_path)
    with run_lock(settings):
        queue = context.Queue()
        process = context.Process(target=child_attempt, args=(str(tmp_path), queue))
        process.start()
        try:
            assert queue.get(timeout=10) is False
            process.join(10)
            assert process.exitcode == 0
        finally:
            if process.is_alive():
                process.terminate()
                process.join(5)
            queue.close()
            queue.join_thread()
    with try_run_lock(settings) as acquired:
        assert acquired


def test_fresh_cli_help_imports_both_windows_lock_modules_without_fcntl():
    script = textwrap.dedent("""\
        import builtins
        import subprocess
        import sys
        from types import SimpleNamespace

        original = builtins.__import__
        project_modules = {"litgleaner.cli_shared", "litgleaner.infra.db.backup"}
        imported_windows = set()
        sys.modules.pop("fcntl", None)

        def importing(name, globals=None, locals=None, fromlist=(), level=0):
            caller = (globals or {}).get("__name__", "")
            if name == "fcntl":
                raise ModuleNotFoundError("No module named 'fcntl'")
            if name == "sys" and caller in project_modules:
                return SimpleNamespace(platform="win32")
            if name == "msvcrt" and caller in project_modules:
                imported_windows.add(caller)
                return SimpleNamespace(LK_NBLCK=2, LK_LOCK=1, LK_UNLCK=0, locking=lambda *_: None)
            return original(name, globals, locals, fromlist, level)

        builtins.__import__ = importing
        from litgleaner.cli import app
        from typer.testing import CliRunner
        result = CliRunner().invoke(app, ["--help"])
        assert result.exit_code == 0, result.output
        assert imported_windows == project_modules
        assert "fcntl" not in sys.modules
        print("fresh Windows CLI help passed")
    """)
    result = subprocess.run(
        [sys.executable, "-c", script], capture_output=True, text=True, timeout=15, check=False
    )
    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == "fresh Windows CLI help passed"
