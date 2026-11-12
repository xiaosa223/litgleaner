"""自动嵌入只计算新论文，并把禁止下载落实到嵌入调用边界。"""

from dataclasses import replace

import pytest

from litgleaner.application import embeddings, memory_admission, reader
from litgleaner.domain.embedding import EmbeddingBatch
from litgleaner.domain.errors import ConfigError
from tests.e2e.embedding_helpers import LocalEmbedder
from tests.e2e.memory_helpers import add


class ReadyEmbedder(LocalEmbedder):
    def __init__(self):
        super().__init__()
        self.allow_download = []

    def embed(self, texts, *, allow_download=True):
        self.allow_download.append(allow_download)
        return EmbeddingBatch(self.model, tuple((1.0, 0.0, 0.0) for _ in texts))


def test_selected_reembed_never_touches_old_or_other_new_papers(harness):
    model = ReadyEmbedder()
    harness.ctx = replace(harness.ctx, embedder=model)
    old, new, other = (add(harness, name) for name in ("old", "new", "other"))
    result = embeddings.reembed_papers(harness.ctx, (new,))
    assert result.plan.total == 1 and result.created == 1
    assert result.plan.pending_paper_ids == (new,)
    assert harness.ctx.store.embeddings.for_paper(old) == ()
    assert harness.ctx.store.embeddings.for_paper(other) == ()
    assert len(harness.ctx.store.embeddings.for_paper(new)) == 1
    assert model.allow_download == [False] and harness.llm_calls() == 0
    assert embeddings.reembed_papers(harness.ctx, (new,)).reused == 1
    assert model.allow_download == [False]


def test_missing_model_does_not_call_embed_or_prepare_assets(harness):
    model = ReadyEmbedder()
    model.model = replace(model.model, available=False, producer_version=None)
    harness.ctx = replace(harness.ctx, embedder=model)
    paper_id = add(harness, "new")
    with pytest.raises(ConfigError, match="不会下载"):
        embeddings.reembed_papers(harness.ctx, (paper_id,))
    assert model.allow_download == []
    memory_admission._local_vector(harness.ctx, paper_id)
    assert model.allow_download == []


def test_describe_to_embed_race_still_forbids_download(harness, monkeypatch, caplog):
    model = ReadyEmbedder()
    harness.ctx = replace(harness.ctx, embedder=model)
    paper_id = add(harness, "new")

    def lost_assets(texts, *, allow_download=True):
        assert allow_download is False
        raise ConfigError("模型文件已消失")

    monkeypatch.setattr(model, "embed", lost_assets)
    reader.set_reading_state(harness.ctx, paper_id, "reading", None)
    memory_admission._local_vector(harness.ctx, paper_id)
    assert harness.ctx.store.embeddings.for_paper(paper_id) == ()
    assert harness.llm_calls() == 0
