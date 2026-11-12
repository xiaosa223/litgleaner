"""耐久首次入库事件；显式用户写入登记，按需后台准备和核实。"""

import logging
from collections.abc import Iterable, Mapping
from typing import Any

from litgleaner.application.context import AppContext
from litgleaner.application.embeddings import reembed_papers
from litgleaner.application.memory_admission_facts import prepare_facts
from litgleaner.application.references import refresh
from litgleaner.application.relate import queue_prepared
from litgleaner.application.relate_input import configuration, prepare_input
from litgleaner.application.stages.base import Stage
from litgleaner.domain.errors import ConfigError, InvalidRequest
from litgleaner.domain.hashing import stable_hash
from litgleaner.ports.store import JobSpec

logger = logging.getLogger(__name__)


def register(ctx: AppContext, paper_id: str) -> None:
    """在调用方的用户写入事务中登记；唯一入库记录防止退出再入触发。"""
    if ctx.store.admissions.get(paper_id) is not None:
        return
    event = ctx.store.admissions.add(paper_id, configuration(ctx), ctx.now_iso())
    ctx.store.jobs.enqueue(
        JobSpec(f"memory_admit:{paper_id}", "memory_admit", {"paper_id": paper_id, "id": event.id}),
        ctx.now_iso(),
    )


def _local_vector(ctx: AppContext, paper_id: str) -> None:
    if ctx.embedder is None or not ctx.embedder.describe().available:
        return
    try:
        reembed_papers(ctx, (paper_id,))
    except ConfigError as error:
        logger.info("首次入库本地向量未就绪 %s：%s", paper_id, error)


def _references(ctx: AppContext, paper_id: str) -> None:
    try:
        refresh(ctx, paper_id)
    except InvalidRequest as error:
        logger.info("首次入库参考文献缺少可查询来源 %s：%s", paper_id, error)


class MemoryAdmissionStage(Stage):
    name = "memory_admit"

    def plan(self, ctx: AppContext) -> Iterable[JobSpec]:
        return ()

    def process(self, ctx: AppContext, payload: Mapping[str, Any]) -> None:
        paper_id = payload["paper_id"]
        event = ctx.store.admissions.get(paper_id)
        if event is None or event.baseline or event.id != payload.get("id"):
            logger.info("首次入库事件不存在或属于基线：%s", paper_id)
            return
        if not any(paper.id == paper_id for paper in ctx.store.embeddings.snapshot().papers):
            logger.info("首次入库论文已退出文库，停止旧事件：%s", paper_id)
            return
        if stable_hash(event.configuration) != stable_hash(configuration(ctx)):
            logger.info("首次入库配置已变化，停止旧事件：%s", paper_id)
            return
        ready = prepare_facts(ctx, paper_id)
        _references(ctx, paper_id)
        _local_vector(ctx, paper_id)
        if not ready:
            logger.info("首次入库缺少可核实原文或前置已终止：%s", paper_id)
            return
        if event.configuration.get("producer") is None:
            logger.info("首次入库未配置核实模型，本次只保存事实：%s", paper_id)
            return
        prepared = prepare_input(ctx, paper_id, eligible_ids=ctx.store.admissions.before(event.id))
        queue_prepared(ctx, paper_id, prepared, scope_id=f"memory_admit:{event.id}")
