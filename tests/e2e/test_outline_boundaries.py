"""费用前护栏同时约束截断证据、模型正文与冻结来源附件。"""

import copy
import json

import pytest

from litgleaner.application import outline
from litgleaner.application.stages.outline import OutlineStage
from litgleaner.domain.errors import PermanentError
from tests.e2e.outline_helpers import TOPIC, add_read_paper, generate, setup_outline


@pytest.mark.parametrize(
    "boundary",
    [
        "method_text",
        "method_quote",
        "dataset_text",
        "dataset_quote",
        "dataset_count",
        "part_unverified",
    ],
)
def test_missing_or_truncated_method_dataset_evidence_cannot_support_gap(harness, boundary):
    ctx, _ids = setup_outline(harness, count=0)
    quote = {"text": "Alpha method", "quote": "We use alpha.", "page": 0, "verified": True}
    data = {"method": quote.copy(), "datasets": [{**quote, "text": "Alpha dataset"}]}
    if boundary.startswith("method"):
        data["method"][boundary.removeprefix("method_")] = "x" * 501
    elif boundary in ("dataset_text", "dataset_quote"):
        data["datasets"][0][boundary.removeprefix("dataset_")] = "x" * 501
    elif boundary == "dataset_count":
        data["datasets"] *= 9
    else:
        data["datasets"].append({**data["datasets"][0], "verified": False})
    ids = [add_read_paper(harness, "Boundary alpha", evidence_data=data)]
    assert ctx.store.papers.get_extraction(ids[0]).data == data
    harness.llm.overrides["outline"] = {
        "groups": [{"name": "group", "description": "short", "goal_ids": [], "paper_ids": ids}],
        "gaps": [{"description": "从不完整输入猜测的缺口", "paper_ids": ids}],
    }
    done = generate(ctx)
    request = json.loads(harness.llm.calls[-1].user.split("\n", 1)[1].rsplit("\n", 1)[0])
    assert request["papers"][0]["eligible_gap_evidence"] is False
    assert request["papers"][0]["extraction_stale"] is False
    assert done.result.gaps == () and done.result.groups[0].paper_ids == tuple(ids)
    assert any("证据不完整" in warning for warning in done.result.warnings)
    assert harness.llm_calls("outline") == 1


@pytest.mark.parametrize("part", ["attachment", "record", "input_json", "eligibility", "goals"])
def test_inconsistent_frozen_queue_payload_is_rejected_before_any_model_call(harness, part):
    ctx, _ids = setup_outline(harness, count=1)
    queued = outline.queue(ctx, TOPIC, outline.preview(ctx, TOPIC).plan_hash)
    p = copy.deepcopy(dict(ctx.store.jobs.get(queued.job_key).payload))
    if part == "attachment":
        p["papers"][0]["bibtex"] = "an invented source citation"
    elif part == "record":
        data = json.loads(p["inputs"])
        data["papers"][0]["method"]["text"] = "an invented method"
        p["inputs"] = json.dumps(data)
    elif part == "input_json":
        p["inputs"] = "{"
    elif part == "eligibility":
        p["eligible_gap_ids"] = []
    else:
        p["goal_ids"] = ["unknown"]
    with pytest.raises(PermanentError, match="重新预览"):
        OutlineStage().process(ctx, p)
    assert harness.llm_calls("outline") == 0
    assert ctx.store.topic_outlines.history(TOPIC) == ()
