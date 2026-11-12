"""实际本地嵌入适配器的自动模式在资产竞态中也不会走下载路径。"""

import shutil

import pytest
from tests.contracts.test_local_embedder import local
from tests.contracts.test_local_embedder import sdk as mock_sdk

from litgleaner.adapters.embeddings import assets
from litgleaner.domain.errors import ConfigError

sdk = mock_sdk


def test_no_download_rejects_unprepared_model_before_loading_sdk(tmp_path, sdk):
    instance = local(tmp_path)
    with pytest.raises(ConfigError, match="不会下载"):
        instance.embed(("Paper",), allow_download=False)
    assert sdk.calls == [] and not instance.root.exists()


def test_ready_assets_can_compute_with_no_download(tmp_path, sdk):
    instance = local(tmp_path)
    instance.embed(("Explicit preparation",))
    before = len(sdk.calls)
    assert instance.embed(("New paper",), allow_download=False).vectors == ((1.0, 2.0, 3.0),)
    assert len(sdk.calls) == before
    assert all(call.get("local_files_only") for call in sdk.calls if "specific_model_path" in call)


def test_assets_removed_after_describe_are_not_downloaded_automatically(tmp_path, sdk):
    instance = local(tmp_path)
    instance.embed(("Prepare",))
    assert instance.describe().available
    shutil.rmtree(instance.root)
    sdk.calls.clear()
    with pytest.raises(ConfigError, match="不会下载"):
        instance.embed(("New paper",), allow_download=False)
    assert sdk.calls == [] and not instance.root.exists()


def test_damaged_assets_after_describe_never_reenter_prepare_download(tmp_path, sdk):
    instance = local(tmp_path)
    instance.embed(("Prepare",))
    assert instance.describe().available
    manifest = assets.read(instance.root, instance.model, instance.dimensions)
    directory = assets.within(instance.root, manifest.directory)
    (directory / "onnx/model.onnx").unlink()
    sdk.calls.clear()
    with pytest.raises(ConfigError):
        instance.embed(("New paper",), allow_download=False)
    assert sdk.calls == []
