"""按需加载 CPU 嵌入器；启动和描述不导入推理运行库。"""

import importlib
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from importlib import metadata
from numbers import Real
from pathlib import Path
from threading import Lock
from typing import Any, Protocol, cast

from litgleaner.adapters.embeddings import assets
from litgleaner.domain.embedding import EmbeddingBatch, EmbeddingModel, pack_vector
from litgleaner.domain.errors import ConfigError
from litgleaner.domain.hashing import stable_hash

_CONTRACT = "local-cpu-embedding-v1"
_PROVIDERS = ("CPUExecutionProvider",)
_REQUIRED_RUNTIME = ("fastembed", "onnxruntime", "tokenizers", "numpy")
_OPTIONAL_RUNTIME = ("onnxruntime-gpu", "onnxruntime-directml")


@dataclass
class _Runtime:
    loaded: str | None = None


_RUNTIME = _Runtime()
_RUNTIME_LOCK = Lock()


class _Engine(Protocol):
    model: Any

    def embed(
        self, documents: tuple[str, ...], *, batch_size: int
    ) -> Iterable[Iterable[float]]: ...


class _Factory(Protocol):
    def __call__(self, **kwargs: Any) -> _Engine: ...
    def list_supported_models(self) -> list[dict[str, Any]]: ...


def _runtime_version() -> str | None:
    versions: dict[str, str | None] = {}
    for name in (*_REQUIRED_RUNTIME, *_OPTIONAL_RUNTIME):
        try:
            versions[name] = metadata.version(name)
        except metadata.PackageNotFoundError:
            versions[name] = None
        except (OSError, ValueError):
            return None
    if any(not versions[name] for name in _REQUIRED_RUNTIME):
        return None
    return stable_hash(versions)


def _sdk() -> _Factory:
    return cast(_Factory, importlib.import_module("fastembed").TextEmbedding)


def _factory(runtime: str) -> _Factory:
    with _RUNTIME_LOCK:
        if _RUNTIME.loaded is not None and runtime != _RUNTIME.loaded:
            raise ConfigError("本地嵌入运行库已变化，请重启进程后重新计算")
        factory = _sdk()
        if _runtime_version() != runtime:
            _RUNTIME.loaded = "changed-during-import"
            raise ConfigError("本地嵌入运行库在加载期间变化，请重启进程后重新计算")
        _RUNTIME.loaded = runtime
        return factory


def _description(factory: _Factory, model: str, dimensions: int) -> Mapping[str, Any]:
    found = next(
        (item for item in factory.list_supported_models() if item.get("model") == model), None
    )
    if found is None or type(found.get("dim")) is not int or found["dim"] != dimensions:
        raise ValueError("unsupported_embedding_model_or_dimensions")
    return found


def _required(root: Path, directory: Path, description: Mapping[str, Any]) -> None:
    names = [description.get("model_file"), "tokenizer.json", "tokenizer_config.json"]
    additional = description.get("additional_files") or []
    if not isinstance(additional, list):
        raise ValueError("invalid_supported_model_description")
    for name in (*names, *additional):
        if not isinstance(name, str):
            raise ValueError("required_embedding_model_file_missing")
        path = assets.within(root, str(directory.relative_to(assets.resolved(root)) / name))
        if not path.is_file() or not path.stat().st_size:
            raise ValueError("required_embedding_model_file_missing")


def _model_directory(engine: _Engine, root: Path) -> Path:
    """SDK 的本地目录尚无公共访问器，将私有属性边界隔离在适配器内。"""
    directory = assets.resolved(Path(engine.model._model_dir))
    base = assets.resolved(root)
    if directory == base or not directory.is_relative_to(base):
        raise ValueError("downloaded_embedding_model_outside_cache")
    return directory


def _vectors(
    engine: _Engine, texts: tuple[str, ...], batch_size: int
) -> tuple[tuple[float, ...], ...]:
    result = []
    for vector in engine.embed(texts, batch_size=batch_size):
        values = tuple(vector)
        if any(isinstance(value, bool) or not isinstance(value, Real) for value in values):
            raise ValueError("embedding_result_values_not_numeric")
        result.append(tuple(float(value) for value in values))
    if len(result) != len(texts):
        raise ValueError("embedding_result_count_mismatch")
    return tuple(result)


class LocalEmbedder:
    def __init__(
        self,
        model: str,
        dimensions: int,
        cache_dir: Path,
        *,
        threads: int = 1,
        batch_size: int = 16,
    ) -> None:
        if not model.strip() or type(dimensions) is not int or not 1 <= dimensions <= 65536:
            raise ConfigError("本地嵌入模型名称或维度无效")
        if type(threads) is not int or threads < 1 or type(batch_size) is not int or batch_size < 1:
            raise ConfigError("本地嵌入线程数或批大小无效")
        self.model, self.dimensions = model, dimensions
        self.threads, self.batch_size = threads, batch_size
        self.configuration_version = stable_hash(
            {
                "contract": _CONTRACT,
                "model": model,
                "dimensions": dimensions,
                "providers": _PROVIDERS,
                "threads": threads,
                "batch_size": batch_size,
            }
        )
        self.root = cache_dir / self.configuration_version
        self._engine: _Engine | None = None
        self._engine_version: str | None = None
        self._lock = Lock()

    def _info(
        self, runtime: str | None, manifest: assets.Assets | None, reason: str | None
    ) -> EmbeddingModel:
        version = (
            stable_hash(
                {
                    "configuration": self.configuration_version,
                    "runtime": runtime,
                    "assets": manifest.version,
                }
            )
            if runtime is not None and manifest is not None
            else None
        )
        return EmbeddingModel(
            self.model,
            self.dimensions,
            self.configuration_version,
            version,
            version is not None and reason is None,
            reason,
        )

    def describe(self) -> EmbeddingModel:
        runtime = _runtime_version()
        if runtime is None:
            return self._info(None, None, "缺少 memory 可选依赖，请先安装后执行本地重算")
        try:
            manifest = assets.read(self.root, self.model, self.dimensions)
        except (OSError, ValueError, TypeError, KeyError, RuntimeError):
            return self._info(
                runtime, None, "本地模型资产缺失、变化或清单无效，请检查缓存后显式重算"
            )
        reason = None if manifest else "本地模型尚未准备，显式重算时下载"
        if _RUNTIME.loaded is not None and runtime != _RUNTIME.loaded:
            reason = "本地嵌入运行库已变化，请重启进程后重新计算"
        return self._info(runtime, manifest, reason)

    def _prepare(
        self, factory: _Factory, description: Mapping[str, Any], manifest: assets.Assets | None
    ) -> assets.Assets:
        if manifest is not None:
            return assets.verify(self.root, manifest)
        self.root.mkdir(parents=True, exist_ok=True)
        engine = factory(
            model_name=self.model,
            cache_dir=str(self.root / "download"),
            providers=list(_PROVIDERS),
            threads=self.threads,
            lazy_load=True,
        )
        directory = _model_directory(engine, self.root)
        _required(self.root, directory, description)
        return assets.capture(self.root, directory, self.model, self.dimensions)

    def _engine_for(
        self,
        factory: _Factory,
        manifest: assets.Assets,
        runtime: str,
        description: Mapping[str, Any],
    ) -> _Engine:
        version = self._info(runtime, manifest, None).producer_version
        if self._engine is not None and self._engine_version == version:
            return self._engine
        directory = assets.within(self.root, manifest.directory)
        _required(self.root, directory, description)
        self._engine = factory(
            model_name=self.model,
            cache_dir=str(self.root / "download"),
            providers=list(_PROVIDERS),
            threads=self.threads,
            lazy_load=False,
            specific_model_path=str(directory),
            local_files_only=True,
        )
        self._engine_version = version
        return self._engine

    def _compute(
        self, texts: tuple[str, ...], runtime: str, allow_download: bool
    ) -> EmbeddingBatch:
        manifest = assets.read(self.root, self.model, self.dimensions, check_stat=False)
        if manifest is None and not allow_download:
            raise ConfigError("本地嵌入模型未就绪；自动入库不会下载资产")
        factory = _factory(runtime)
        description = _description(factory, self.model, self.dimensions)
        manifest = self._prepare(factory, description, manifest)
        engine = self._engine_for(factory, manifest, runtime, description)
        vectors = _vectors(engine, texts, self.batch_size)
        for vector in vectors:
            pack_vector(vector, self.dimensions)
        manifest = assets.verify(self.root, manifest)
        assets.write(self.root, manifest)
        return EmbeddingBatch(self._info(runtime, manifest, None), vectors)

    def embed(self, texts: tuple[str, ...], *, allow_download: bool = True) -> EmbeddingBatch:
        if not texts:
            return EmbeddingBatch(self.describe(), ())
        if any(not isinstance(text, str) or not text.strip() for text in texts):
            raise ConfigError("本地嵌入输入必须是非空文本")
        runtime = _runtime_version()
        if runtime is None:
            raise ConfigError("缺少 memory 可选依赖，请安装后执行 memory reembed --execute")
        try:
            with self._lock:
                return self._compute(texts, runtime, allow_download)
        except ConfigError:
            raise
        except Exception as error:
            raise ConfigError("本地嵌入计算或模型校验失败，请检查可选依赖与模型缓存") from error
