"""真实入库、初筛、评估和引文事实构成阅读路线，接口本身没有外部调用。"""

from dataclasses import replace

import pytest
from fastapi.testclient import TestClient

from litgleaner.application.reading_route import reading_route
from litgleaner.domain.citation import Citation
from litgleaner.domain.identity import Alias
from litgleaner.server.app import create_app
from litgleaner.server.security import LocalSecurity
from tests.e2e.conftest import Harness, paper


def _ids(harness):
    return {
        title: next(iter(harness.ctx.store.papers.find_ids_by_aliases((Alias("doi", doi),))))
        for title, doi in (
            ("Review", "10.1000/review"),
            ("Foundation", "10.1000/foundation"),
            ("Representative", "10.1000/representative"),
            ("Latest", "10.1000/latest"),
            ("Outsider", "10.1000/outsider"),
        )
    }


def _cite(harness, citing, cited, position, source="source-one"):
    harness.ctx.store.citations.add(
        Citation(
            citing,
            position,
            "A reference",
            source,
            0,
            cited,
            None,
            (),
            (),
            (),
            None,
            harness.ctx.now_iso(),
        )
    )


def _prepare(harness):
    harness.write_papers(
        [
            paper("Review", 5, publication_types=["Review"]),
            paper("Foundation", 5, published_at="2000-01-01"),
            paper("Representative", 5, published_at="2010-01-01"),
            paper("Latest", 3),
            paper("Outsider", 5, marker="[[irrelevant]]"),
        ]
    )
    assert harness.run().ok
    ids = _ids(harness)
    _cite(harness, ids["Representative"], ids["Foundation"], "one")
    _cite(harness, ids["Representative"], ids["Foundation"], "two", "source-two")
    _cite(harness, ids["Outsider"], ids["Foundation"], "three")
    _cite(harness, ids["Foundation"], ids["Foundation"], "self")
    return ids


def test_reading_route_uses_only_current_topic_distinct_nonself_citing_papers(harness: Harness):
    ids = _prepare(harness)
    before = harness.llm_calls()
    view = reading_route(harness.ctx, "alpha-topic", limit_per_group=1)
    assert view.total == 4 and not view.ungrouped
    assert [group.papers[0].paper_id for group in view.groups] == [
        ids["Review"],
        ids["Foundation"],
        ids["Representative"],
        ids["Latest"],
    ]
    assert view.groups[1].papers[0].topic_citations == 1
    assert view.groups[2].papers[0].reading_value == 100
    offline = replace(harness.ctx, llm=replace(harness.ctx.llm, routes={}))
    assert reading_route(offline, "alpha-topic", limit_per_group=1) == view
    assert harness.llm_calls() == before


def test_route_api_is_authenticated_validates_bounds_and_has_no_model_calls(harness: Harness):
    _prepare(harness)
    security = LocalSecurity(48133, launch_token="reading-route")
    before = harness.llm_calls()
    with TestClient(
        create_app(harness.ctx, security, None), base_url="http://127.0.0.1:48133"
    ) as client:
        path = "/api/topics/alpha-topic/reading-route"
        assert client.get(path).status_code == 401
        client.get("/launch", params={"token": "reading-route"})
        response = client.get(path, params={"limit_per_group": 1})
        assert response.status_code == 200 and response.json()["total"] == 4
        assert response.json()["groups"][0]["papers"][0]["publication_types"] == ["Review"]
        assert client.get(path, params={"limit_per_group": 0}).status_code == 422
        assert client.get("/api/topics/missing/reading-route").status_code == 404
    assert harness.llm_calls() == before


def test_changed_profile_does_not_reuse_old_triage_or_silently_rescreen(harness: Harness):
    _prepare(harness)
    before = harness.llm_calls()
    harness.edit_yaml("topics/alpha-topic.yaml", lambda data: data.update(background="Changed"))
    harness.rebuild()
    assert reading_route(harness.ctx, "alpha-topic").total == 0
    assert harness.llm_calls() == before


@pytest.mark.parametrize("change", ["language", "budget", "policy", "semantic"])
def test_changed_score_inputs_hide_stale_scores_without_model_calls(harness: Harness, change):
    _prepare(harness)
    before = harness.llm_calls()
    context = harness.ctx
    if change in {"language", "budget"}:
        settings = (
            {"reading_language": "en"} if change == "language" else {"assess_token_budget": 1000}
        )
        context = replace(context, settings=replace(context.settings, **settings))
    else:
        rubric = context.rubrics["default"]
        dimensions = tuple(
            dim.model_copy(
                update={"weight": dim.weight + (1 if index == 0 else -1 if index == 1 else 0)}
            )
            if change == "policy"
            else dim.model_copy(update={"question": dim.question + " updated"})
            for index, dim in enumerate(rubric.dimensions)
        )
        changed = rubric.model_copy(update={"dimensions": dimensions})
        context = replace(context, rubrics={**context.rubrics, "default": changed})
    view = reading_route(context, "alpha-topic")
    papers = [paper for group in view.groups for paper in group.papers] + list(view.ungrouped)
    assert view.total == 4 and all(paper.reading_value is None for paper in papers)
    assert harness.llm_calls() == before
