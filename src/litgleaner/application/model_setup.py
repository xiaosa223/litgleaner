"""首次模型操作的配置与真实能力探测；只发送固定的短测试内容。"""

from contextlib import suppress
from dataclasses import dataclass

from jsonschema import Draft202012Validator

from litgleaner.application.context import AppContext
from litgleaner.domain.errors import BudgetExceeded, InvalidRequest, LitGleanerError
from litgleaner.ports.llm import (
    LLM,
    LLMCall,
    LLMRequest,
    ModelCatalog,
    ModelInfo,
    StreamDone,
    TextDelta,
    TextRequest,
)
from litgleaner.ports.model_settings import (
    CatalogDraft,
    ModelDraft,
    ModelSettings,
    ModelSettingsView,
    probe_output_tokens,
)

_SCHEMA = {
    "type": "object",
    "properties": {"ok": {"type": "boolean", "const": True}},
    "required": ["ok"],
    "additionalProperties": False,
}
_TASK = "configuration_test"


@dataclass(frozen=True)
class ProbeResult:
    ok: bool
    capabilities: tuple[str, ...]
    cost_usd: float | None  # None：有调用价格未知（ADR 0013）
    estimated_usd: float | None  # None：草稿没有价格
    code: str | None = None


def status(settings: ModelSettings) -> ModelSettingsView:
    return settings.view()


def save(settings: ModelSettings, draft: ModelDraft) -> ModelSettingsView:
    return settings.save(draft)


def _record(
    ctx: AppContext,
    model: str,
    tokens: tuple[int, int],
    cost: float | None,
    ok: bool,
    *,
    costs: list[float | None],
) -> None:
    costs.append(cost)
    ctx.store.runs.record_llm_call(
        LLMCall(_TASK, model, "connection-probe-1", *tokens, cost, ok), ctx.now_iso()
    )


def _structured(ctx: AppContext, llm: LLM, draft: ModelDraft, costs: list[float | None]) -> None:
    ctx.llm.check_budget()
    result = llm.complete(
        LLMRequest(_TASK, draft.model, 'Return {"ok":true}.', "Connection test.", _SCHEMA)
    )
    valid = Draft202012Validator(_SCHEMA).is_valid(result.data)
    _record(
        ctx,
        result.model,
        (result.input_tokens, result.output_tokens),
        result.cost_usd,
        valid,
        costs=costs,
    )
    if not valid:
        raise ValueError("probe_schema_invalid")


def _stream(ctx: AppContext, llm: LLM, draft: ModelDraft, costs: list[float | None]) -> None:
    ctx.llm.check_budget()
    content = False
    done = False
    for event in llm.stream(TextRequest(_TASK, draft.model, "Reply with OK.", "Connection test.")):
        if isinstance(event, TextDelta):
            content |= bool(event.text.strip())
        elif isinstance(event, StreamDone):
            done = event.error is None
            _record(
                ctx,
                event.model,
                (event.input_tokens, event.output_tokens),
                event.cost_usd,
                done,
                costs=costs,
            )
    if not content or not done:
        raise ValueError("probe_stream_invalid")


def _estimate(draft: ModelDraft) -> float | None:
    """上限估计：最多两次调用，每次输出不超过测试上限；价格未知时无法估计。"""
    if draft.input_price is None or draft.output_price is None:
        return None
    tokens = probe_output_tokens(draft)
    return 2 * (128 * draft.input_price + tokens * draft.output_price) / 1_000_000


def _total(costs: list[float | None]) -> float | None:
    known = [cost for cost in costs if cost is not None]
    return None if len(known) < len(costs) else sum(known)


def list_models(settings: ModelSettings, source: CatalogDraft) -> tuple[ModelInfo, ...]:
    """按草稿的协议、地址、密钥列出可用模型（ADR 0013）。不计费、不写配置。"""
    client = settings.catalog_client(source)
    try:
        if not isinstance(client, ModelCatalog):
            raise InvalidRequest("model_list_unsupported")
        return client.list_models()
    finally:
        with suppress(Exception):
            client.close()


def probe(ctx: AppContext, settings: ModelSettings, draft: ModelDraft) -> ProbeResult:
    ctx.llm.check_budget()
    capabilities: list[str] = []
    costs: list[float | None] = []
    estimated = _estimate(draft)
    llm: LLM | None = None
    try:
        llm = settings.client(draft)
        if set(draft.tasks) - {"translate"}:
            _structured(ctx, llm, draft, costs)
            capabilities.append("structured")
        if "translate" in draft.tasks:
            _stream(ctx, llm, draft, costs)
            capabilities.append("stream")
        return ProbeResult(True, tuple(capabilities), _total(costs), estimated)
    except BudgetExceeded:
        raise
    except Exception as error:
        if isinstance(error, LitGleanerError) and (usage := error.model_usage) is not None:
            _record(
                ctx,
                usage.model,
                (usage.input_tokens, usage.output_tokens),
                usage.cost_usd,
                False,
                costs=costs,
            )
        # 提供方错误可能回显认证头或请求体；界面只收到固定错误码。
        return ProbeResult(
            False, tuple(capabilities), _total(costs), estimated, "model_connection_failed"
        )
    finally:
        if llm is not None:
            with suppress(Exception):
                llm.close()
