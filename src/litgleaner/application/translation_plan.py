"""整篇翻译（第 12.3、15.1 节）：先估算，用户确认后排入任务队列，由按需阶段 translate 执行。

- 估算只读缓存和价目表，不调用模型，不产生费用。
- 任务键含译者版本和术语表指纹：同一版本只排一次；换了模型或术语表就是新任务。
- 只翻还没有当前版本译文的段落；旧版本译文照常显示，直到被新译文取代。
"""

from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from typing import Any, Literal

from litgleaner.application.context import AppContext
from litgleaner.application.stages.base import Stage
from litgleaner.application.translation import (
    cache_key,
    paper_glossary,
    paper_paragraphs,
    translate_blocks,
    translator_version,
)
from litgleaner.application.translation_run import TASK
from litgleaner.domain.errors import PermanentError, TransientError
from litgleaner.domain.hashing import stable_hash
from litgleaner.domain.numbered import group_batches
from litgleaner.ports.store import JobSpec

STAGE = "translate"
CHARS_PER_TOKEN = 4.0  # 英文正文的经验值；只用于估算
OUTPUT_RATIO = 1.2  # 译文加编号，按原文 token 的 1.2 倍估
PROMPT_TOKENS_PER_BATCH = 900  # 系统提示词和术语表
_PER_MILLION = 1_000_000

State = Literal["idle", "queued", "retrying", "done", "failed"]


@dataclass(frozen=True)
class TranslationPlan:
    paragraphs: int
    pending: int
    characters: int  # 待翻段落的原文字数
    estimated_usd: float | None  # 不知道模型价格时为 None
    state: State
    error: str | None = None


def _job_key(ctx: AppContext, paper_id: str) -> str:
    glossary = paper_glossary(ctx, paper_id)
    source = stable_hash([p.text for p in paper_paragraphs(ctx, paper_id)])[:16]
    return f"{STAGE}:{paper_id}:{translator_version(ctx)}:{glossary.glossary_hash}:{source}"


def _state(ctx: AppContext, paper_id: str) -> tuple[State, str | None]:
    found = ctx.store.jobs.get(_job_key(ctx, paper_id))
    if found is None:
        return "idle", None
    if found.status == "done":
        return "done", None
    if found.status == "dead":
        return "failed", found.last_error
    return ("retrying" if found.attempts else "queued"), found.last_error


def _estimate(ctx: AppContext, lengths: list[int]) -> float | None:
    route = ctx.llm.routes.get(TASK)
    if route is None:
        return None
    price = route.llm.price(route.model)
    if price is None:
        return None
    tokens_in = sum(lengths) / CHARS_PER_TOKEN
    batches = len(group_batches(lengths))
    total_in = tokens_in + batches * PROMPT_TOKENS_PER_BATCH
    total_out = tokens_in * OUTPUT_RATIO
    return (total_in * price[0] + total_out * price[1]) / _PER_MILLION


def plan(ctx: AppContext, paper_id: str) -> TranslationPlan:
    found = paper_paragraphs(ctx, paper_id)
    glossary = paper_glossary(ctx, paper_id)
    pending = [
        p
        for p in found
        if (cached := ctx.store.translations.get(cache_key(ctx, p, glossary))) is None
        or len(cached.sentences) != len(p.sentences)
    ]
    lengths = [len(p.text) for p in pending]
    state, error = _state(ctx, paper_id)
    return TranslationPlan(
        paragraphs=len(found),
        pending=len(pending),
        characters=sum(lengths),
        estimated_usd=_estimate(ctx, lengths) if pending else 0.0,
        state=state,
        error=error,
    )


def queue(ctx: AppContext, paper_id: str) -> TranslationPlan:
    """用户确认后排队。已完成但仍有段落没翻好时（例如个别段落校验失败）重新排队。"""
    current = plan(ctx, paper_id)
    if current.pending == 0:
        return current
    key = _job_key(ctx, paper_id)
    spec = JobSpec(key=key, stage=STAGE, payload={"paper_id": paper_id, "confirmed_key": key})
    with ctx.store.atomic():
        ctx.store.jobs.enqueue(spec, ctx.now_iso(), reopen=True)
        if (found := ctx.store.jobs.get(key)) is not None and found.status == "dead":
            ctx.store.jobs.reopen(key, ctx.now_iso())  # 用户明确重试，后台不自动重开死信
    return plan(ctx, paper_id)


class TranslatePaperStage(Stage):
    """按需阶段：没有 plan，只处理用户确认过的任务。"""

    name = STAGE

    def plan(self, ctx: AppContext) -> Iterable[JobSpec]:
        return ()

    def process(self, ctx: AppContext, payload: Mapping[str, Any]) -> None:
        paper_id = str(payload["paper_id"])
        if payload.get("confirmed_key") != _job_key(ctx, paper_id):
            raise PermanentError("原文、翻译模型或术语表已经变化，请重新确认整篇翻译")
        indices = [i for p in paper_paragraphs(ctx, paper_id) for i in p.indices]
        if indices:
            for _event in translate_blocks(ctx, paper_id, indices):
                pass  # 译文由翻译流程写入缓存；领读器轮询进度
        if plan(ctx, paper_id).pending:
            raise TransientError("部分段落没有通过译文校验，已保存成功的译文，稍后重试剩余段落")
