"""临时检索向量只用现有模型，任何不完整身份或非法批次都不能流入排序。"""

from dataclasses import replace
from types import SimpleNamespace

import pytest

from litgleaner.application.query_vectors import embed, identity
from litgleaner.domain.embedding import EmbeddingBatch, EmbeddingModel
from litgleaner.domain.errors import ConfigError


class LocalModel:
    def __init__(self):
        self.model = EmbeddingModel("local", 2, "config", "producer", True)
        self.calls = []
        self.vector = (0.1, 0.9)
        self.after = None
        self.batch = None
        self.count = 1

    def describe(self):
        return self.model

    def embed(self, texts, *, allow_download=True):
        self.calls.append((texts, allow_download))
        batch = self.batch or self.model
        if self.after is not None:
            self.model = self.after
        return EmbeddingBatch(batch, tuple(self.vector for _ in range(self.count)))


def test_only_one_local_batch_without_download_and_canonical_float32_values():
    model = LocalModel()
    (vector,) = embed(SimpleNamespace(embedder=model), ("query",), model.model)
    assert model.calls == [(("query",), False)]
    assert vector == pytest.approx(model.vector)
    assert vector != model.vector  # 返回实际float32数值，不混入宿主float64精度。


@pytest.mark.parametrize(
    "changes",
    [
        {"model": "another"},
        {"dimensions": 3},
        {"configuration_version": "other"},
        {"producer_version": "other"},
        {"available": False},
    ],
)
@pytest.mark.parametrize("when", ["before", "after", "batch"])
def test_every_model_identity_field_is_checked_before_after_and_on_batch(changes, when):
    model = LocalModel()
    expected = model.model
    changed = replace(expected, **changes)
    setattr(model, "model" if when == "before" else when, changed)
    with pytest.raises(ConfigError):
        embed(SimpleNamespace(embedder=model), ("query",), expected)
    assert len(model.calls) == (0 if when == "before" else 1)


@pytest.mark.parametrize("count", [0, 2])
def test_wrong_batch_size_is_rejected(count):
    model = LocalModel()
    model.count = count
    with pytest.raises(ConfigError):
        embed(SimpleNamespace(embedder=model), ("query",), model.model)


@pytest.mark.parametrize(
    "vector", [(0.0, 0.0), (float("nan"), 1.0), (float("inf"), 1.0), (1.0,), (True, 1.0)]
)
def test_invalid_dimensions_values_and_zero_norm_are_rejected(vector):
    model = LocalModel()
    model.vector = vector
    with pytest.raises(ValueError):
        embed(SimpleNamespace(embedder=model), ("query",), model.model)


@pytest.mark.parametrize(
    "changes",
    [
        {"available": False},
        {"producer_version": None},
        {"producer_version": ""},
        {"dimensions": 0},
        {"dimensions": 65537},
        {"dimensions": True},
    ],
)
def test_unavailable_or_invalid_model_never_starts_inference(changes):
    model = LocalModel()
    model.model = replace(model.model, **changes)
    with pytest.raises(ConfigError):
        embed(SimpleNamespace(embedder=model), ("query",), model.model)
    assert not model.calls


def test_missing_embedder_and_reason_only_changes_do_not_confuse_model_identity():
    model = LocalModel()
    with pytest.raises(ConfigError):
        embed(SimpleNamespace(embedder=None), ("query",), model.model)
    assert identity(model.model) == identity(replace(model.model, reason="another explanation"))
