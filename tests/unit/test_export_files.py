"""导出文件边界：可移植文件名、独占写入、失败清理和 ZIP 资源释放。"""

from zipfile import ZipFile

import pytest

from litgleaner.domain.errors import InvalidRequest
from litgleaner.domain.export import ExportFile
from litgleaner.infra import export_files


@pytest.mark.parametrize(
    "name",
    [
        "../escape",
        "/absolute",
        "a/../../escape",
        "a\\b",
        "C:/a",
        "a//b",
        "a/./b",
        "CON",
        "NUL.txt",
        "a/COM1.pdf",
        "a.",
    ],
)
def test_both_sinks_reject_nonportable_or_escaping_names_without_creating_files(tmp_path, name):
    entries = [ExportFile(name, b"must not escape")]
    out = tmp_path / "export"
    with pytest.raises(InvalidRequest):
        export_files.write_directory(entries, out)
    with pytest.raises(InvalidRequest):
        export_files.zip_archive(entries)
    assert list(tmp_path.iterdir()) == []


@pytest.mark.parametrize("names", [("A.md", "a.md"), ("a", "a/b"), ("a/b", "a"), ("a", "a")])
def test_duplicate_or_file_directory_collisions_fail_in_both_sinks(tmp_path, names):
    entries = [ExportFile(name, b"data") for name in names]
    with pytest.raises(InvalidRequest):
        export_files.write_directory(entries, tmp_path / "out")
    with pytest.raises(InvalidRequest):
        export_files.zip_archive(entries)
    assert not (tmp_path / "out").exists()


def test_failed_generation_removes_own_files_but_preserves_concurrent_other_files(tmp_path):
    out = tmp_path / "out"

    def broken():
        yield ExportFile("papers/owned.md", b"our incomplete output")
        (out / "another-user-file.txt").write_text("preserve this")
        raise RuntimeError("generation failed")

    with pytest.raises(RuntimeError, match="generation failed"):
        export_files.write_directory(broken(), out)
    assert (out / "another-user-file.txt").read_text() == "preserve this"
    assert not (out / "papers").exists()


def test_zip_failure_closes_temporary_file_before_propagating(monkeypatch):
    original = export_files.tempfile.SpooledTemporaryFile
    created = []

    def capture(*args, **kwargs):
        archive = original(*args, **kwargs)
        created.append(archive)
        return archive

    monkeypatch.setattr(export_files.tempfile, "SpooledTemporaryFile", capture)
    with pytest.raises(InvalidRequest):
        export_files.zip_archive([ExportFile("good.md", b"ok"), ExportFile("../bad", b"no")])
    assert len(created) == 1 and created[0].closed


def test_archive_is_deterministic_and_has_regular_private_files():
    entries = [ExportFile("papers/a.md", "中文内容".encode()), ExportFile("manifest.json", b"{}")]
    with export_files.zip_archive(entries) as first, export_files.zip_archive(entries) as second:
        assert first.read() == second.read()
        first.seek(0)
        with ZipFile(first) as archive:
            assert archive.namelist() == [entry.name for entry in entries]
            for entry in entries:
                assert archive.read(entry.name) == entry.content
                assert archive.getinfo(entry.name).external_attr >> 16 == 0o100600
