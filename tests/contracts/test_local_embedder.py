"""本地嵌入采用 SDK 替身，核对首次准备、资产身份和零副作用描述。"""

import json
import os
import shutil
from importlib import metadata
from pathlib import Path
from types import SimpleNamespace

import pytest

from litgleaner.adapters.embeddings import assets
from litgleaner.adapters.embeddings import fastembed as adapter
from litgleaner.domain.errors import ConfigError

MODEL = "sentence-transformers/paraphrase-multilingual-MiniLM-L12-v2"


@pytest.fixture(autouse=True)
def fresh_process_runtime(monkeypatch):
    monkeypatch.setattr(adapter._RUNTIME, "loaded", None)


class SDK:
    def __init__(self):
        self.calls = []
        self.inputs = []
        self.vectors = None
        self.description = {"model": MODEL, "dim": 3, "model_file": "onnx/model.onnx"}
        self.prepare = None

    def list_supported_models(self):
        return [self.description]

    def __call__(self, **kwargs):
        self.calls.append(kwargs)
        directory = Path(kwargs.get("specific_model_path", Path(kwargs["cache_dir"]) / "snapshot"))
        if "specific_model_path" not in kwargs:
            directory.mkdir(parents=True, exist_ok=True)
            files = {
                "onnx/model.onnx": b"fake model weights",
                "tokenizer.json": b"{}",
                "tokenizer_config.json": b"{}",
                "config.json": b"{}",
            }
            for name, content in files.items():
                path = directory / name
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_bytes(content)
            if self.prepare:
                self.prepare(directory)

        def embed(texts, *, batch_size):
            self.inputs.append((texts, batch_size))
            if self.vectors is not None:
                return self.vectors
            return [(1.0, 2.0, 3.0) for _ in texts]

        return SimpleNamespace(model=SimpleNamespace(_model_dir=directory), embed=embed)


@pytest.fixture
def sdk(monkeypatch):
    factory = SDK()
    monkeypatch.setattr(adapter, "_runtime_version", lambda: "0.8.1")
    monkeypatch.setattr(adapter, "_sdk", lambda: factory)
    return factory


def local(tmp_path, **changes):
    return adapter.LocalEmbedder(MODEL, 3, tmp_path / "models", **changes)


def test_construct_and_describe_never_import_sdk_create_paths_or_load_engine(tmp_path, monkeypatch):
    monkeypatch.setattr(adapter, "_runtime_version", lambda: "0.8.1")

    def forbidden():
        raise AssertionError("描述不应导入 SDK")

    monkeypatch.setattr(adapter, "_sdk", forbidden)
    instance = local(tmp_path)
    info = instance.describe()
    assert info.model == MODEL and info.dimensions == 3
    assert info.configuration_version and info.producer_version is None
    assert not info.available and "未准备" in info.reason
    assert not (tmp_path / "models").exists()
    assert instance.embed(()).vectors == ()
    assert not (tmp_path / "models").exists()


def test_missing_optional_dependency_is_described_without_loading_anything(tmp_path, monkeypatch):
    def missing(_name):
        raise metadata.PackageNotFoundError

    monkeypatch.setattr(adapter.metadata, "version", missing)
    instance = local(tmp_path)
    assert not instance.describe().available
    assert instance.describe().producer_version is None
    with pytest.raises(ConfigError, match="memory 可选依赖"):
        instance.embed(("A paper",))
    assert not instance.root.exists()


def test_explicit_first_embed_prepares_once_and_uses_cpu_local_files_for_inference(tmp_path, sdk):
    instance = local(tmp_path, threads=2, batch_size=4)
    result = instance.embed(("Paper A", "Paper B"))
    assert result.vectors == ((1.0, 2.0, 3.0), (1.0, 2.0, 3.0))
    assert result.model.available and result.model.producer_version
    assert result.model == instance.describe()
    assert sdk.inputs == [(("Paper A", "Paper B"), 4)]
    assert len(sdk.calls) == 2
    first, actual = sdk.calls
    assert first["lazy_load"] is True and "local_files_only" not in first
    assert actual["lazy_load"] is False and actual["local_files_only"] is True
    assert all(
        call["providers"] == ["CPUExecutionProvider"] and call["threads"] == 2 for call in sdk.calls
    )
    assert actual["specific_model_path"].startswith(str(instance.root))
    manifest = json.loads((instance.root / "manifest.json").read_text())
    assert len(manifest["files"]) == 4
    assert all(len(item["sha256"]) == 64 for item in manifest["files"])
    assert all(not Path(item["path"]).is_absolute() for item in manifest["files"])


def test_cached_execution_is_local_only_and_returns_the_actual_stable_producer(tmp_path, sdk):
    instance = local(tmp_path)
    initial = instance.embed(("First",))
    sdk.calls.clear()
    later = instance.embed(("Later",))
    assert initial.model.producer_version == later.model.producer_version
    assert not sdk.calls
    assert sdk.inputs[-1] == (("Later",), 16)


def test_sdk_module_is_imported_only_by_explicit_embedding(tmp_path, monkeypatch):
    factory = SDK()
    imports = []
    original = adapter.importlib.import_module

    def load(name, package=None):
        if name == "fastembed":
            imports.append(name)
            return SimpleNamespace(TextEmbedding=factory)
        return original(name, package)

    monkeypatch.setattr(adapter.importlib, "import_module", load)
    monkeypatch.setattr(adapter.metadata, "version", lambda _name: "0.8.1")
    instance = local(tmp_path)
    assert not instance.describe().available and not imports
    assert instance.embed(("Paper",)).model.available
    assert imports == ["fastembed"]


def test_ready_description_uses_only_manifest_stats_and_dependency_metadata(
    tmp_path, sdk, monkeypatch
):
    instance = local(tmp_path)
    result = instance.embed(("First",))

    def forbidden(*_args, **_kwargs):
        raise AssertionError("只读描述不能读取权重内容或加载运行库")

    monkeypatch.setattr(adapter, "_sdk", forbidden)
    monkeypatch.setattr(assets, "capture", forbidden)
    assert instance.describe() == result.model


def test_move_cache_does_not_change_configuration_or_asset_content_version(tmp_path, sdk):
    first = local(tmp_path)
    original = first.embed(("Paper",)).model
    relocated = adapter.LocalEmbedder(MODEL, 3, tmp_path / "elsewhere")
    shutil.copytree(first.root, relocated.root)
    assert relocated.describe() == original


def test_runtime_upgrade_changes_producer_but_not_configuration(tmp_path, sdk, monkeypatch):
    instance = local(tmp_path)
    original = instance.embed(("Paper",)).model
    monkeypatch.setattr(adapter, "_runtime_version", lambda: "0.8.2")
    upgraded = instance.describe()
    assert not upgraded.available and "重启" in upgraded.reason
    assert upgraded.producer_version != original.producer_version
    assert upgraded.configuration_version == original.configuration_version
    monkeypatch.setattr(adapter._RUNTIME, "loaded", None)  # 模拟重新启动后的新进程。
    restarted = local(tmp_path).describe()
    assert restarted.available and restarted.producer_version == upgraded.producer_version


@pytest.mark.parametrize(
    "distribution", ["fastembed", "onnxruntime", "tokenizers", "numpy", "onnxruntime-gpu"]
)
def test_each_inference_distribution_version_affects_the_producer(
    tmp_path, monkeypatch, distribution
):
    versions = {
        "fastembed": "0.8.1",
        "onnxruntime": "1.24.3",
        "tokenizers": "0.22.0",
        "numpy": "2.5.4",
    }
    factory = SDK()

    def version(name):
        if name not in versions:
            raise metadata.PackageNotFoundError
        return versions[name]

    monkeypatch.setattr(adapter.metadata, "version", version)
    monkeypatch.setattr(adapter, "_sdk", lambda: factory)
    instance = local(tmp_path)
    original = instance.embed(("Paper",)).model
    versions[distribution] = "new-version"
    changed = instance.describe()
    assert not changed.available and "重启" in changed.reason
    assert changed.producer_version != original.producer_version
    assert changed.configuration_version == original.configuration_version
    assert len(factory.calls) == 2


def test_incomplete_inference_distribution_metadata_is_not_ready(tmp_path, monkeypatch):
    def version(name):
        if name == "onnxruntime":
            raise metadata.PackageNotFoundError
        return "0.8.1"

    monkeypatch.setattr(adapter.metadata, "version", version)
    assert not local(tmp_path).describe().available


def test_inference_options_change_configuration_while_cache_location_does_not(tmp_path):
    original = local(tmp_path)
    assert (
        original.configuration_version
        == adapter.LocalEmbedder(MODEL, 3, tmp_path / "other").configuration_version
    )
    assert original.configuration_version != local(tmp_path, threads=2).configuration_version
    assert original.configuration_version != local(tmp_path, batch_size=2).configuration_version


@pytest.mark.parametrize("change", ["missing", "size", "mtime", "added"])
def test_asset_stat_change_removes_ready_state_without_importing_sdk(
    tmp_path, sdk, monkeypatch, change
):
    instance = local(tmp_path)
    instance.embed(("Paper",))
    manifest = assets.read(instance.root, MODEL, 3)
    directory = assets.within(instance.root, manifest.directory)
    path = directory / "tokenizer.json"
    if change == "missing":
        path.unlink()
    elif change == "size":
        path.write_bytes(b"changed")
    elif change == "mtime":
        state = path.stat()
        os.utime(path, ns=(state.st_atime_ns, state.st_mtime_ns + 1000000))
    else:
        (directory / "new.json").write_text("{}")
    monkeypatch.setattr(adapter, "_sdk", lambda: pytest.fail("describe imported SDK"))
    info = instance.describe()
    assert not info.available and info.producer_version is None


def test_checksum_change_with_preserved_stat_is_rejected_before_inference(tmp_path, sdk):
    instance = local(tmp_path)
    instance.embed(("Paper",))
    manifest = assets.read(instance.root, MODEL, 3)
    path = assets.within(instance.root, manifest.directory) / "tokenizer.json"
    state = path.stat()
    path.write_bytes(b"[]")
    os.utime(path, ns=(state.st_atime_ns, state.st_mtime_ns))
    assert instance.describe().available  # 描述仅检查状态；实际计算必须核对内容。
    before = len(sdk.inputs)
    with pytest.raises(ConfigError, match="校验失败"):
        instance.embed(("New paper",))
    assert len(sdk.inputs) == before


def test_explicit_embedding_refreshes_timestamps_when_content_is_unchanged(tmp_path, sdk):
    instance = local(tmp_path)
    initial = instance.embed(("Paper",)).model
    manifest = assets.read(instance.root, MODEL, 3)
    path = assets.within(instance.root, manifest.directory) / "tokenizer.json"
    state = path.stat()
    os.utime(path, ns=(state.st_atime_ns, state.st_mtime_ns + 1000000))
    assert not instance.describe().available
    assert instance.embed(("Paper",)).model == initial
    assert instance.describe() == initial


@pytest.mark.parametrize(
    "vectors",
    [
        [],
        [(1.0, 2.0)],
        [(0.0, 0.0, 0.0)],
        [(float("nan"), 1.0, 2.0)],
        [(float("inf"), 1.0, 2.0)],
        [(True, 1.0, 2.0)],
        [("1", 2.0, 3.0)],
    ],
)
def test_invalid_sdk_batch_never_publishes_a_ready_manifest(tmp_path, sdk, vectors):
    sdk.vectors = vectors
    instance = local(tmp_path)
    with pytest.raises(ConfigError):
        instance.embed(("Paper",))
    assert not (instance.root / "manifest.json").exists()
    assert not instance.describe().available


@pytest.mark.parametrize("change", ["model", "dimensions", "file", "outside"])
def test_unsupported_or_incomplete_model_is_rejected(tmp_path, sdk, change):
    if change == "model":
        sdk.description["model"] = "another-model"
    elif change == "dimensions":
        sdk.description["dim"] = 4
    elif change == "file":
        sdk.prepare = lambda directory: (directory / "tokenizer.json").unlink()
    else:

        def outside(directory):
            path = directory / "tokenizer.json"
            path.unlink()
            (tmp_path / "outside").write_text("{}")
            path.symlink_to(tmp_path / "outside")

        sdk.prepare = outside
    instance = local(tmp_path)
    with pytest.raises(ConfigError):
        instance.embed(("Paper",))
    assert not (instance.root / "manifest.json").exists()


def test_hub_snapshot_symlink_to_blob_inside_controlled_cache_is_supported(tmp_path, sdk):
    def shared_blob(directory):
        path = directory / "tokenizer.json"
        blob = directory.parent / "blob"
        blob.write_bytes(path.read_bytes())
        path.unlink()
        path.symlink_to(blob)

    sdk.prepare = shared_blob
    instance = local(tmp_path)
    assert instance.embed(("Paper",)).model.available
    assert instance.describe().available


@pytest.mark.parametrize("change", ["path", "digest", "entry", "identity", "oversize"])
def test_manifest_identity_paths_and_entries_are_checked_before_use(tmp_path, sdk, change):
    instance = local(tmp_path)
    instance.embed(("Paper",))
    path = instance.root / "manifest.json"
    raw = json.loads(path.read_text())
    if change == "path":
        raw["directory"] = "../outside"
    elif change == "digest":
        raw["files"][0]["sha256"] = "invalid"
    elif change == "entry":
        raw["files"][0]["size"] = True
    elif change == "identity":
        raw["model"] = "another-model"
    else:
        raw["padding"] = "x" * (1024 * 1024)
    path.write_text(json.dumps(raw))
    before = len(sdk.calls)
    assert not instance.describe().available
    with pytest.raises(ConfigError):
        instance.embed(("Paper",))
    assert len(sdk.calls) == before


@pytest.mark.parametrize("raw", ["[]", "{}", "not-json", '{"contract":"other"}'])
def test_bad_manifest_does_not_become_current_or_trigger_download_in_describe(tmp_path, sdk, raw):
    instance = local(tmp_path)
    instance.root.mkdir(parents=True)
    (instance.root / "manifest.json").write_text(raw)
    assert not instance.describe().available and not sdk.calls
    with pytest.raises(ConfigError):
        instance.embed(("Paper",))
    assert not sdk.calls


@pytest.mark.parametrize("text", ["", "  ", None, 5])
def test_invalid_input_does_not_prepare_model(tmp_path, sdk, text):
    instance = local(tmp_path)
    with pytest.raises(ConfigError):
        instance.embed((text,))
    assert not sdk.calls and not instance.root.exists()
