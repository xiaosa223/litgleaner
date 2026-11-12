"""缓存异常只影响本地嵌入可用性，实际生产者不会因热升级而误记。"""

import shutil
from concurrent.futures import ThreadPoolExecutor
from threading import Event, Lock
from time import sleep

import pytest
from tests.contracts.test_local_embedder import MODEL, SDK, local

from litgleaner.adapters.embeddings import assets
from litgleaner.adapters.embeddings import fastembed as adapter
from litgleaner.domain.errors import ConfigError


@pytest.fixture(autouse=True)
def fresh_process_runtime(monkeypatch):
    monkeypatch.setattr(adapter._RUNTIME, "loaded", None)


@pytest.fixture
def sdk(monkeypatch):
    factory = SDK()
    monkeypatch.setattr(adapter, "_runtime_version", lambda: "0.8.1")
    monkeypatch.setattr(adapter, "_sdk", lambda: factory)
    return factory


class ConcurrentSDK(SDK):
    def __init__(self):
        super().__init__()
        self.active = self.peak = 0
        self.guard = Lock()

    def __call__(self, **kwargs):
        engine = super().__call__(**kwargs)
        original = engine.embed

        def embed(texts, *, batch_size):
            with self.guard:
                self.active += 1
                self.peak = max(self.peak, self.active)
            try:
                sleep(0.01)
                return original(texts, batch_size=batch_size)
            finally:
                with self.guard:
                    self.active -= 1

        engine.embed = embed
        return engine


@pytest.mark.parametrize("target", ["manifest", "directory", "asset", "root"])
def test_cyclic_cache_symlinks_are_unavailable_and_never_load_the_sdk(
    tmp_path, sdk, monkeypatch, target
):
    instance = local(tmp_path)
    instance.embed(("Paper",))
    manifest = assets.read(instance.root, MODEL, 3)
    directory = assets.within(instance.root, manifest.directory)
    path = {
        "manifest": instance.root / "manifest.json",
        "directory": directory,
        "asset": directory / "tokenizer.json",
        "root": instance.root,
    }[target]
    if path.is_dir():
        shutil.rmtree(path)
    else:
        path.unlink()
    path.symlink_to(path)
    imports = []

    def forbidden():
        imports.append(True)
        raise AssertionError("损坏的缓存不能触发 SDK 加载")

    monkeypatch.setattr(adapter, "_sdk", forbidden)
    info = instance.describe()
    assert not info.available and info.producer_version is None
    with pytest.raises(ConfigError):
        instance.embed(("Another paper",))
    assert not imports


@pytest.mark.parametrize("target", ["directory", "asset"])
def test_cyclic_downloaded_model_paths_never_start_inference_or_publish_manifest(
    tmp_path, sdk, target
):
    def corrupt(directory):
        path = directory if target == "directory" else directory / "tokenizer.json"
        if path.is_dir():
            shutil.rmtree(path)
        else:
            path.unlink()
        path.symlink_to(path)

    sdk.prepare = corrupt
    instance = local(tmp_path)
    with pytest.raises(ConfigError):
        instance.embed(("Paper",))
    assert len(sdk.calls) == 1 and not sdk.inputs
    assert not (instance.root / "manifest.json").exists()
    assert not instance.describe().available


def test_repeated_batches_reuse_the_actual_loaded_inference_engine(tmp_path, sdk):
    instance = local(tmp_path)
    batches = [instance.embed((f"Batch {index}",)) for index in range(4)]
    assert len(sdk.calls) == 2
    assert len(sdk.inputs) == 4
    assert len({batch.model.producer_version for batch in batches}) == 1
    assert all(batch.model == instance.describe() for batch in batches)


def test_changed_runtime_requires_restart_including_new_adapter_instances(
    tmp_path, sdk, monkeypatch
):
    instance = local(tmp_path)
    original = instance.embed(("Paper",)).model
    manifest = (instance.root / "manifest.json").read_bytes()
    monkeypatch.setattr(adapter, "_runtime_version", lambda: "upgraded-runtime")
    for candidate in (instance, local(tmp_path)):
        info = candidate.describe()
        assert not info.available and "重启" in info.reason
        assert info.producer_version != original.producer_version
        with pytest.raises(ConfigError, match="重启"):
            candidate.embed(("Another paper",))
    assert len(sdk.calls) == 2 and len(sdk.inputs) == 1
    assert (instance.root / "manifest.json").read_bytes() == manifest


def test_runtime_change_during_sdk_import_requires_restart_before_any_model_load(
    tmp_path, sdk, monkeypatch
):
    def changed():
        monkeypatch.setattr(adapter, "_runtime_version", lambda: "upgraded-runtime")
        return sdk

    monkeypatch.setattr(adapter, "_sdk", changed)
    instance = local(tmp_path)
    with pytest.raises(ConfigError, match=r"加载期间变化.*重启"):
        instance.embed(("Paper",))
    with pytest.raises(ConfigError, match="重启"):
        local(tmp_path).embed(("Another paper",))
    assert not sdk.calls and not instance.root.exists()


def test_concurrent_batches_on_one_adapter_share_one_serial_inference_engine(tmp_path, monkeypatch):
    factory = ConcurrentSDK()
    monkeypatch.setattr(adapter, "_runtime_version", lambda: "0.8.1")
    monkeypatch.setattr(adapter, "_sdk", lambda: factory)
    instance = local(tmp_path)
    with ThreadPoolExecutor(max_workers=4) as pool:
        batches = tuple(pool.map(lambda index: instance.embed((f"Paper {index}",)), range(12)))
    assert len(factory.calls) == 2 and len(factory.inputs) == 12
    assert factory.peak == 1
    assert len({batch.model.producer_version for batch in batches}) == 1


def test_parallel_adapters_cannot_overwrite_a_runtime_identity_during_import(
    tmp_path, sdk, monkeypatch
):
    importing, release, started = Event(), Event(), Event()
    imports = []

    def load():
        imports.append(True)
        if len(imports) == 1:
            importing.set()
            assert release.wait(5)
        return sdk

    def second():
        started.set()
        return local(tmp_path / "second").embed(("Second paper",))

    monkeypatch.setattr(adapter, "_sdk", load)
    with ThreadPoolExecutor(max_workers=2) as pool:
        first = pool.submit(local(tmp_path / "first").embed, ("First paper",))
        try:
            assert importing.wait(5)
            monkeypatch.setattr(adapter, "_runtime_version", lambda: "upgraded-runtime")
            another = pool.submit(second)
            assert started.wait(5)
            with pytest.raises(TimeoutError):
                another.result(timeout=0.03)
        finally:
            release.set()
        for future in (first, another):
            with pytest.raises(ConfigError, match="重启"):
                future.result(timeout=5)
    assert len(imports) == 1 and not sdk.calls
