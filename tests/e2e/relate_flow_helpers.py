"""冻结模型响应的端到端工具，不绕过真实输入、网关和SQLite。"""

import json
from dataclasses import replace

from litgleaner.application import relate
from litgleaner.application.pipeline import run_on_demand
from litgleaner.ports.llm import LLMResponse


def respond(harness, monkeypatch, *, mode="positive", cost=0.01, during=None):
    original = type(harness.llm).complete

    def complete(self, request):
        if request.task != "relate":
            return original(self, request)
        if during is not None:
            during()
        # user内容经网关隔离；这里只使用先前真正准备且冻结的双侧证据。
        pairs = json.loads(
            request.user.removeprefix("<untrusted_paper>\n").removesuffix("\n</untrusted_paper>")
        )["pairs"]
        values = []
        for pair in pairs:
            a, b = pair["paper_a"]["paper_id"], pair["paper_b"]["paper_id"]
            value = {
                "paper_a": a,
                "paper_b": b,
                "kind": "uses_method_of",
                "explanation": "Both original passages support the relation.",
            }
            for side in ("a", "b"):
                record = pair[f"paper_{side}"]
                value[f"evidence_{side}"] = {
                    "paper_id": record["paper_id"],
                    "doc_key": record["doc_key"],
                    "page": record["fragments"][0]["page"],
                    "quote": record["fragments"][0]["quote"],
                }
            if mode == "unrelated":
                value.update(kind=None, evidence_a=None, evidence_b=None)
            elif mode == "bad-quote":
                value["evidence_b"]["quote"] = (
                    "This is a fabricated quotation absent from both papers."
                )
            if mode != "empty":
                values.append(value)
        return LLMResponse({"relations": values}, request.model, 100, 50, cost)

    monkeypatch.setattr(type(harness.llm), "complete", complete)


def generate(harness, paper_id):
    plan = relate.preview(harness.ctx, paper_id)
    queued = relate.queue(harness.ctx, paper_id, plan.plan_hash)
    report = run_on_demand(harness.ctx)
    assert report.ok, report
    result = relate.status(harness.ctx, paper_id, queued.job_key)
    assert result.status == "done" and result.result is not None
    return result


def change_prompt(harness):
    route = harness.ctx.llm.routes["relate"]
    prompt = replace(
        route.prompt, text=route.prompt.text + "\nInspect both original passages carefully."
    )
    harness.ctx = replace(
        harness.ctx,
        llm=replace(
            harness.ctx.llm,
            routes={**harness.ctx.llm.routes, "relate": replace(route, prompt=prompt)},
        ),
    )
