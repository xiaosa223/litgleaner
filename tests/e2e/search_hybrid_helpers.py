"""真实搜索库与可核验本地查询嵌入替身。"""

from dataclasses import asdict, replace

from sqlalchemy import insert

from litgleaner.application import embeddings
from litgleaner.application.embedding_inputs import inputs
from litgleaner.domain.embedding import EmbeddingBatch, EmbeddingModel, PaperEmbedding, pack_vector
from litgleaner.domain.errors import ConfigError
from litgleaner.infra.db.schema import paper_embedding
from tests.e2e.search_normalization_helpers import forbidden, paper


class QueryEmbedder:
    def __init__(self):
        self.model = EmbeddingModel("global-local", 3, "config", "producer", True)
        self.calls = []
        self.vectors = {}
        self.invalid = None
        self.descriptions = 0
        self.change_at = None

    def describe(self):
        self.descriptions += 1
        if self.change_at == self.descriptions:
            self.model = replace(self.model, producer_version="changed")
        return self.model

    def embed(self, texts, *, allow_download=True):
        self.calls.append((texts, allow_download))
        if self.invalid == "raise":
            raise ConfigError("private/model/file")
        vectors = tuple(self.vectors.get(text, (1.0, 0.0, 0.0)) for text in texts)
        if self.invalid in {"zero", "nan", "dimension"}:
            value = {
                "zero": (0.0, 0.0, 0.0),
                "nan": (float("nan"), 0.0, 0.0),
                "dimension": (1.0, 0.0),
            }[self.invalid]
            vectors = tuple(value for _ in texts)
        elif self.invalid == "count":
            vectors = ()
        elif self.invalid == "batch_identity":
            return EmbeddingBatch(replace(self.model, configuration_version="other"), vectors)
        return EmbeddingBatch(self.model, vectors)


def setup(harness, *, names=("Unrelated mechanisms",), cache=True):
    model = QueryEmbedder()
    harness.ctx = replace(harness.ctx, embedder=model)
    ids = tuple(paper(harness, name, library=True) for name in names)
    if cache:
        embeddings.reembed(harness.ctx)
    model.calls.clear()
    model.descriptions = 0
    return model, ids


def block_external(harness, monkeypatch):
    monkeypatch.setattr(harness.ctx.store._db, "write", forbidden)
    for route in harness.ctx.llm.routes.values():
        monkeypatch.setattr(route.llm, "complete", forbidden)
        monkeypatch.setattr(route.llm, "stream", forbidden)
    for provider in harness.ctx.sources.values():
        monkeypatch.setattr(provider, "fetch", forbidden)


def cache_vectors(harness, overrides):
    ctx = harness.ctx
    snapshot = ctx.store.embeddings.snapshot()
    model = ctx.embedder.describe()
    sources = inputs(ctx, snapshot.papers, snapshot.fulltexts, snapshot.extractions)
    for source in sources.values():
        vector = overrides.get(source.paper_id, (1.0, 0.0, 0.0))
        raw = vector if isinstance(vector, bytes) else pack_vector(vector, 3)
        record = PaperEmbedding(
            source.paper_id,
            source.input_hash,
            model.producer_version,
            model.configuration_version,
            model.model,
            model.dimensions,
            source.input_text,
            raw,
            source.source_extraction_id,
            ctx.now_iso(),
        )
        if isinstance(vector, bytes):
            # 模拟旧版本/外部写入，保留数据库长度约束及不可变触发器。
            ctx.store._db.write(insert(paper_embedding).values(**asdict(record)))
        else:
            ctx.store.embeddings.add(record)
    ctx.embedder.descriptions = 0
