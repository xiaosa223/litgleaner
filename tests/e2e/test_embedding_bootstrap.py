"""可选本地模型配置独立加载，启动不依赖 SDK 或模型资产。"""

from pathlib import Path

import pytest
import yaml

from litgleaner import bootstrap
from litgleaner.adapters.embeddings import fastembed as adapter
from litgleaner.bootstrap_args import BuildArgs, _llm_key
from litgleaner.domain.errors import ConfigError
from litgleaner.infra.config import load_config


def write(harness, changes):
    path = harness.root / "config/memory.yaml"
    path.write_text(yaml.safe_dump({"embedding": changes}))
    return path


def test_missing_memory_file_uses_default_without_sdk_import_or_asset_writes(harness, monkeypatch):
    monkeypatch.setattr(adapter, "_sdk", lambda: pytest.fail("bootstrap imported SDK"))
    ctx = harness.rebuild()
    loaded = load_config(harness.root / "config")
    assert ctx.embedder is not None
    assert ctx.embedder.model == "sentence-transformers/paraphrase-multilingual-MiniLM-L12-v2"
    assert ctx.embedder.dimensions == 384
    assert ctx.settings.embedding.max_input_chars == 8000
    assert ctx.settings.embedding.batch_size == 16
    assert loaded.embedding_cache_dir == loaded.database_path.parent / "models/embeddings"
    assert not loaded.embedding_cache_dir.exists()


def test_config_overrides_cache_and_generic_runtime_settings(harness):
    write(
        harness,
        {
            "model": "some-local-model",
            "dimensions": 12,
            "cache_dir": "data/custom-models",
            "max_input_chars": 1000,
            "batch_size": 3,
            "threads": 2,
        },
    )
    ctx = harness.rebuild()
    loaded = load_config(harness.root / "config")
    assert ctx.embedder.model == "some-local-model" and ctx.embedder.dimensions == 12
    assert ctx.settings.embedding.max_input_chars == 1000 and ctx.settings.embedding.batch_size == 3
    assert loaded.embedding_cache_dir == harness.root / "data/custom-models"
    assert not loaded.embedding_cache_dir.exists()


def test_disabled_embedder_does_not_require_a_registered_adapter(harness, monkeypatch):
    write(harness, {"enabled": False, "adapter": "unavailable"})
    monkeypatch.setattr(adapter, "_sdk", lambda: pytest.fail("disabled embedder imported SDK"))
    assert harness.rebuild().embedder is None


def test_unknown_enabled_adapter_is_reported_as_configuration_error(harness):
    write(harness, {"adapter": "unavailable"})
    with pytest.raises(ConfigError, match="未知的本地嵌入"):
        harness.rebuild()


@pytest.mark.parametrize(
    "changes",
    [
        {"model": " "},
        {"adapter": " "},
        {"cache_dir": " "},
        {"dimensions": 0},
        {"dimensions": True},
        {"dimensions": 65537},
        {"dimensions": "384"},
        {"threads": 0},
        {"threads": True},
        {"batch_size": 0},
        {"batch_size": True},
        {"batch_size": 257},
        {"max_input_chars": 99},
        {"max_input_chars": "8000"},
        {"enabled": "false"},
        {"unknown": True},
    ],
)
def test_memory_configuration_rejects_invalid_or_unknown_fields(harness, changes):
    path = write(harness, changes)
    with pytest.raises(ConfigError, match=r"memory\.yaml"):
        load_config(path.parent)


def test_memory_root_rejects_unknown_fields(harness):
    (harness.root / "config/memory.yaml").write_text("unexpected: true\n")
    with pytest.raises(ConfigError, match=r"memory\.yaml"):
        harness.rebuild()


def test_template_registers_the_default_embedding_adapter():
    template = Path(__file__).parents[2] / "config/memory.yaml"
    assert yaml.safe_load(template.read_text())["embedding"]["adapter"] in bootstrap.EMBEDDERS


def test_build_args_and_key_helper_remain_importable_from_bootstrap():
    assert bootstrap.BuildArgs is BuildArgs and bootstrap._llm_key is _llm_key
