"""重提取消费冻结清单；精确缓存并发出现时直接复用。"""

from collections.abc import Mapping
from dataclasses import replace
from typing import Any

from litgleaner.application.context import AppContext
from litgleaner.application.extraction_schema import EXTRACT_SCHEMA
from litgleaner.application.reextract import _result_id
from litgleaner.application.reextract_input import (
    DocumentCache,
    PreparedExtraction,
    configuration,
    prepare_paper,
)
from litgleaner.application.stages.extract import persist_extraction
from litgleaner.domain.errors import PermanentError
from litgleaner.domain.hashing import stable_hash
from litgleaner.domain.language import language_name


def _configuration_guard(ctx: AppContext, payload: Mapping[str, Any]) -> None:
    if any(payload.get(key) != value for key, value in configuration(ctx).items()):
        raise PermanentError("重提取配置已变化，请重新预览后确认")


def _prepare(ctx: AppContext, payload: Mapping[str, Any]) -> PreparedExtraction | None:
    data = ctx.store.reextract_snapshot((payload["paper_id"],))
    if _result_id(data, payload) is not None:
        return None
    frozen = {key: value for key, value in payload.items() if key != "scope_id"}
    if payload.get("scope_id") != f"reextract:{payload['paper_id']}:{stable_hash(frozen)}":
        raise PermanentError("重提取任务作用域与冻结清单不符")
    _configuration_guard(ctx, payload)
    paper = next((paper for paper in data.papers if paper.id == payload["paper_id"]), None)
    if paper is None:
        raise PermanentError("重提取论文已不可用")
    current = prepare_paper(ctx, data, paper, DocumentCache(ctx))
    if current is None or stable_hash(current.manifest) != stable_hash(frozen):
        raise PermanentError("重提取来源、实际正文或基线已变化，请重新预览后确认")
    return current


def process(ctx: AppContext, payload: Mapping[str, Any]) -> None:
    prepared = _prepare(ctx, payload)
    if prepared is None:
        return
    with ctx.store.atomic():
        _configuration_guard(ctx, payload)
        if ctx.store.reextract_revision((payload["paper_id"],)) != payload["metadata_revision"]:
            snapshot = ctx.store.reextract_snapshot((payload["paper_id"],))
            if _result_id(snapshot, payload) is not None:
                return
            raise PermanentError("调用前重提取元数据已变化，请重新预览")
        _configuration_guard(ctx, payload)
    result = replace(ctx.llm, routes=dict(ctx.llm.routes), scope_id=payload["scope_id"]).call(
        "extract",
        {"output_language": language_name(payload["reading_language"])},
        payload["inputs"],
        EXTRACT_SCHEMA,
        paper_id=payload["paper_id"],
    )
    current = _prepare(ctx, payload)
    if current is None:
        return
    with ctx.store.atomic():
        _configuration_guard(ctx, payload)
        if ctx.store.reextract_revision((payload["paper_id"],)) != payload["metadata_revision"]:
            fresh = ctx.store.reextract_snapshot((payload["paper_id"],))
            if _result_id(fresh, payload) is not None:
                return
            raise PermanentError("重提取期间元数据已变化，未保存新事实")
        _configuration_guard(ctx, payload)
        persist_extraction(
            ctx,
            payload["paper_id"],
            payload["input_hash"],
            payload["producer_version"],
            data=result.data,
            document=current.document,
            source=current.source,
        )
        saved = ctx.store.papers.get_extraction(payload["paper_id"])
        if (
            saved is None
            or saved.id == payload["latest_extraction_id"]
            or saved.input_hash != payload["input_hash"]
            or saved.extractor_version != payload["producer_version"]
        ):
            raise PermanentError("新提取未写入，已撤销本次概念与事实")
