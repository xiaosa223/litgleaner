"""只消费冻结关系任务；双侧核验后原子保留批次和不可变证据。"""

from collections.abc import Iterable, Mapping
from dataclasses import replace
from typing import Any

from litgleaner.application.context import AppContext
from litgleaner.application.relate import save_batch
from litgleaner.application.relate_input import (
    RelateInput,
    configuration,
    prepare_input,
    producer_version,
    refresh_documents,
    source_hash,
    validate_result,
)
from litgleaner.application.relate_schema import RELATE_SCHEMA
from litgleaner.application.stages.base import Stage
from litgleaner.domain.errors import InvalidRequest, NotFound, PermanentError
from litgleaner.domain.evidence import verify_quote
from litgleaner.domain.language import language_name
from litgleaner.ports.store import JobSpec


def _configuration_guard(ctx: AppContext, p: Mapping[str, Any]) -> None:
    if (
        dict(configuration(ctx)) != p["configuration"]
        or producer_version(ctx) != p["producer_version"]
        or ctx.settings.reading_language != p["reading_language"]
        or ctx.settings.relate.token_budget != p["token_budget"]
    ):
        raise PermanentError("关系核实配置已变化，请重新预览后确认")


def _prepare_frozen(ctx: AppContext, p: Mapping[str, Any]) -> RelateInput:
    _configuration_guard(ctx, p)
    try:
        prepared = prepare_input(ctx, p["paper_id"], eligible_ids=tuple(p["eligible_ids"]))
    except (ValueError, LookupError, InvalidRequest, NotFound) as error:
        raise PermanentError("关系核实来源已不可用，请重新预览") from error
    if (
        prepared.text != p["inputs"]
        or prepared.input_hash != p["input_hash"]
        or prepared.source_hashes != p["source_hashes"]
        or list(prepared.paper_ids) != list(p["input_paper_ids"])
        or prepared.tokens > p["token_budget"]
    ):
        raise PermanentError("关系核实候选或已发送原文已变化，请重新预览后确认")
    return prepared


def _sources_guard(ctx: AppContext, p: Mapping[str, Any]) -> None:
    """返回后只检查已发送来源；用户反馈竞争由最终写事务保护。"""
    _configuration_guard(ctx, p)
    data = ctx.store.memory_snapshot()
    papers = {paper.id: paper for paper in data.papers}
    extractions = {row.paper_id: row for row in sorted(data.extractions, key=lambda row: row.id)}
    hashes = {
        id_: source_hash(papers[id_], data.fulltexts.get(id_), extractions.get(id_))
        for id_ in p["input_paper_ids"]
        if id_ in papers
    }
    if hashes != p["source_hashes"]:
        raise PermanentError("关系核实期间已发送来源变化，未写入判断，请重新预览")


def _context_guard(frozen: RelateInput, current: RelateInput) -> None:
    for id_, source in frozen.sources.items():
        fresh = current.sources.get(id_)
        if fresh is None:
            raise PermanentError("关系核实期间原文归档不可读，未写入判断")
        for sent in source.source.sources:
            page = "\n".join(
                "\n".join(filter(None, (block.caption, block.text)))
                for block in fresh.document.blocks
                if block.page == sent.page
            )
            if not verify_quote(sent.text, page):
                raise PermanentError("关系核实期间已发送原文变化，未写入判断，请重新预览")


class RelateStage(Stage):
    name = "relate"

    def plan(self, ctx: AppContext) -> Iterable[JobSpec]:
        return ()

    def process(self, ctx: AppContext, payload: Mapping[str, Any]) -> None:
        p = payload
        if (
            ctx.store.relations.by_fingerprint(
                p["paper_id"], p["input_hash"], p["producer_version"]
            )
            is not None
        ):
            return
        ctx.llm.require_route("relate")
        prepared = _prepare_frozen(ctx, p)
        if not prepared.pairs_count:
            with ctx.store.atomic():
                _sources_guard(ctx, p)
                save_batch(ctx, p, ctx.llm.require_route("relate").model, (), ())
            return
        result = replace(ctx.llm, scope_id=p["scope_id"]).call(
            "relate",
            {"output_language": language_name(p["reading_language"])},
            p["inputs"],
            RELATE_SCHEMA,
            paper_id=p["paper_id"],
        )
        refreshed = refresh_documents(ctx, prepared)
        _context_guard(prepared, refreshed)
        relations, warnings = validate_result(ctx, refreshed, result.data)
        with ctx.store.atomic():
            _sources_guard(ctx, p)
            save_batch(ctx, p, result.model, relations, warnings)
