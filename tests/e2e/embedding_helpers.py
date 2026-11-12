"""显式本地嵌入验收替身，模型文件或网络都不参与。"""

from dataclasses import replace

from litgleaner.domain.embedding import EmbeddingBatch, EmbeddingModel


class LocalEmbedder:
    def __init__(self):
        self.model = EmbeddingModel("local-test", 3, "config-v1", "producer-v1", True)
        self.calls = []
        self.vectors = {}
        self.hook = None
        self.prepare = False

    def describe(self):
        return self.model

    def embed(self, texts):
        if self.prepare:
            self.model = replace(
                self.model, available=True, producer_version="producer-v1", reason=None
            )
        model = self.model
        self.calls.append(texts)
        if self.hook:
            self.hook(texts)
        vectors = tuple(self.vectors.get(text, (1.0, 0.0, 0.0)) for text in texts)
        return EmbeddingBatch(model, vectors)


def setup(harness, *, batch_size=16):
    model = LocalEmbedder()
    settings = replace(
        harness.ctx.settings,
        embedding=harness.ctx.settings.embedding.model_copy(update={"batch_size": batch_size}),
    )
    harness.ctx = replace(harness.ctx, embedder=model, settings=settings)
    return model
