"""首次模型操作的配置与真实能力探测；只发送固定的短测试内容。"""

from contextlib import suppress
from dataclasses import dataclass

from jsonschema import Draft202012Validator

from litgleaner.application.context import AppContext
from litgleaner.domain.errors import BudgetExceeded, LitGleanerError
from litgleaner.ports.llm import LLM, LLMRequest, StreamDone, TextDelta, TextRequest
from litgleaner.ports.model_settings import ModelDraft, ModelSettings, ModelSettingsView
from litgleaner.ports.store import LLMCall

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
    cost_usd: float
    estimated_usd: float
    code: str | None = None


def status(settings: ModelSettings) -> ModelSettingsView:
    return settings.view()


def save(settings: ModelSettings, draft: ModelDraft) -> ModelSettingsView:
    return settings.save(draft)


def _record(
    ctx: AppContext,
    model: str,
    tokens: tuple[int, int],
    cost: float,
    ok: bool,
    *,
    costs: list[float],
) -> None:
    costs.append(cost)
    ctx.store.runs.record_llm_call(
        LLMCall(_TASK, model, "connection-probe-1", *tokens, cost, ok), ctx.now_iso()
    )


def _structured(ctx: AppContext, llm: LLM, draft: ModelDraft, costs: list[float]) -> None:
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


def _stream(ctx: AppContext, llm: LLM, draft: ModelDraft, costs: list[float]) -> None:
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


def probe(ctx: AppContext, settings: ModelSettings, draft: ModelDraft) -> ProbeResult:
    ctx.llm.check_budget()
    capabilities: list[str] = []
    costs: list[float] = []
    estimated = (128 * draft.input_price + 256 * draft.output_price) / 1_000_000
    llm: LLM | None = None
    try:
        llm = settings.client(draft)
        if set(draft.tasks) - {"translate"}:
            _structured(ctx, llm, draft, costs)
            capabilities.append("structured")
        if "translate" in draft.tasks:
            _stream(ctx, llm, draft, costs)
            capabilities.append("stream")
        return ProbeResult(True, tuple(capabilities), sum(costs), estimated)
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
            False, tuple(capabilities), sum(costs), estimated, "model_connection_failed"
        )
    finally:
        if llm is not None:
            with suppress(Exception):
                llm.close()
