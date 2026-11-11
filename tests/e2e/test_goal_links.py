"""评估同时生成目标关联，并保留失败核验结果而不改变维度计分。"""

import re

from litgleaner.application.documents import load_chunks
from litgleaner.application.reading_route import reading_route
from litgleaner.domain.document import Block, NormalizedDocument
from litgleaner.domain.identity import Alias
from tests.e2e.conftest import Harness, paper


def test_assessment_persists_goal_links_and_routes_show_only_existing_goals(harness: Harness):
    harness.write_papers([paper("Goal Links", 5)])
    dimensions = {
        key: {"level": 5, "reason": "有充分依据", "quote": None}
        for key in ("relevance", "novelty", "evidence", "reusability")
    }
    harness.llm.overrides["assess"] = {
        "dimensions": dimensions,
        "goal_links": [
            {"goal_id": "g1", "reuse": "借鉴问题建模", "quote": "We study the problem.", "page": 0},
            {
                "goal_id": "g1",
                "reuse": "另一页的错误定位",
                "quote": "We study the problem.",
                "page": 1,
            },
            {"goal_id": "unknown", "reuse": "虚构目标", "quote": None, "page": None},
        ],
    }
    assert harness.run().ok
    paper_id = next(
        iter(harness.ctx.store.papers.find_ids_by_aliases((Alias("doi", "10.1000/goal-links"),)))
    )
    scored = harness.ctx.store.judgments.scored_for_paper(paper_id)[0]
    assert scored.score == 100
    assert [link.verified for link in scored.assessment.goal_links] == [True, False]
    route = reading_route(harness.ctx, "alpha-topic")
    chosen = next(paper for group in route.groups for paper in group.papers)
    assert chosen.goal_links == scored.assessment.goal_links
    assert harness.ctx.store.runs.llm_cost_for_paper(paper_id) == 0.003


def test_old_results_are_not_regenerated_to_fill_missing_goal_links(harness: Harness):
    harness.write_papers([paper("No Links", 5)])
    assert harness.run().ok
    before = harness.llm_calls("assess")
    assert harness.run().ok
    route = reading_route(harness.ctx, "alpha-topic")
    assert all(not paper.goal_links for group in route.groups for paper in group.papers)
    assert harness.llm_calls("assess") == before


def test_cross_page_chunk_preserves_actual_later_page_marker_and_verified_quote(
    harness, monkeypatch
):
    quote = "Our method reduces memory consumption without sacrificing accuracy."

    def parsed(_pdf, sha256):
        return NormalizedDocument(
            1,
            sha256,
            "fake_parser@1",
            "Cross Page",
            (
                Block("heading", "Abstract", 0),
                Block("text", "An alpha study.", 0),
                Block("heading", "Introduction", 0),
                Block("text", "We introduce our method.", 0),
                Block("text", quote, 1),
            ),
            (),
            "ok",
        )

    monkeypatch.setattr(harness.ctx.parser, "_document", parsed)
    harness.write_papers([paper("Cross Page", 5)])
    harness.llm.overrides["assess"] = {
        "dimensions": {
            key: {"level": 5, "reason": "有充分依据", "quote": None}
            for key in ("relevance", "novelty", "evidence", "reusability")
        },
        "goal_links": [{"goal_id": "g1", "reuse": "借鉴内存压缩", "quote": quote, "page": 1}],
    }
    assert harness.run().ok
    (paper_id,) = harness.ctx.store.judgments.relevant_papers(
        "alpha-topic", harness.ctx.topics["alpha-topic"].topic_hash
    )
    state = harness.ctx.store.papers.get_fulltext(paper_id)
    assert any(
        chunk.page_start == 0 and chunk.page_end == 1
        for chunk in load_chunks(harness.ctx, state.doc_key).chunks
    )
    request = next(call for call in harness.llm.calls if call.task == "assess")
    assert re.search(r"\[page=1 [a-z]+\]\n" + re.escape(quote), request.user)
    (link,) = harness.ctx.store.judgments.scored_for_paper(paper_id)[0].assessment.goal_links
    assert link.page == 1 and link.verified
