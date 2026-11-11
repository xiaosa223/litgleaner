"""综述 Markdown 使用保存附件，不让模型短说明与来源文字突破文本边界。"""

import re

import pytest

from litgleaner.domain.comparison_table import ComparisonRow, render_table
from litgleaner.domain.export_references import render_references
from litgleaner.domain.identity import Alias
from litgleaner.domain.outline import OutlineGap, OutlineGroup, OutlinePaper, TopicOutline
from litgleaner.domain.outline_render import render_outline
from litgleaner.domain.paper import Paper


def attachment(id_, title=None, verified=True):
    title = title or "Original " + id_
    source = Paper(
        id_,
        title,
        "titlekey",
        None,
        ("Original Author",),
        "Source Venue",
        None,
        "2025-01-01",
        "2026-11-11T00:00:00Z",
    )
    aliases = (Alias("doi", "10.1234/" + id_),) if verified else ()
    references = render_references(((source, aliases),))
    table = render_table(
        (ComparisonRow(id_, title, None, False, None, None, False, None, (), {}),), "md"
    )
    return OutlinePaper(
        paper_id=id_,
        title=title,
        table_markdown=table,
        bibtex=references.bibtex,
        csl_json=references.csl_json,
        needs_verification=not verified,
    )


def result(**changes):
    return TopicOutline(
        **{
            "id": "outline-1",
            "topic_id": "topic",
            "topic_hash": "topic-hash",
            "input_hash": "input-hash",
            "producer_version": "outline-v1",
            "model": "fake",
            "reading_language": "zh-CN",
            "token_budget": 12000,
            "created_at": "2026-11-11T00:00:00Z",
            "scope_id": "outline:topic",
            "considered_paper_ids": ("p1", "p2", "p3", "omitted"),
            "input_paper_ids": ("p1", "p2", "p3"),
            "source_hashes": {"p1": "h1", "p2": "h2", "p3": "h3"},
            "goal_ids": ("g1",),
            "groups": (
                OutlineGroup(
                    name="方法组",
                    description="模型短说明",
                    goal_ids=("g1",),
                    paper_ids=("p1", "p2"),
                ),
            ),
            "gaps": (OutlineGap(description="当前集合的方法比较有待核对", paper_ids=("p1",)),),
            "papers": (attachment("p1"), attachment("p2", verified=False), attachment("p3")),
            "ungrouped": ("p3", "omitted"),
            "warnings": ("保留生成时记录",),
            **changes,
        }
    )


def outside_fences(text):
    output, fence = [], None
    for line in text.splitlines():
        if fence is None and (match := re.fullmatch(r"(`{3,})(?:bibtex|json)", line)):
            fence = match.group(1)
        elif fence is not None and line == fence:
            fence = None
        elif fence is None:
            output.append(line)
    assert fence is None
    return "\n".join(output)


def test_saved_reference_format_warnings_are_visible_and_safely_escaped():
    item = result()
    first = item.papers[0].model_copy(
        update={
            "reference_warnings": (
                "日期不完整 <script>alert(1)</script> [a](https://example.test)",
            )
        }
    )
    item = item.model_copy(update={"papers": (first, *item.papers[1:])})
    text = outside_fences(render_outline(item))
    assert "来源格式警告：日期不完整" in text
    assert "<script>" not in text and "[a](" not in text and "https://" not in text


def test_outline_merges_generated_table_headers_and_uses_exact_saved_citations():
    item = result()
    text = render_outline(item)
    assert "# 综述骨架" in text and "模型分组说明：模型短说明" in text
    assert "对应目标编号：g1" in text
    assert text.count("| 论文标题 | 论文编号 |") == 1
    assert text.splitlines().count(item.papers[0].table_markdown.splitlines()[1]) == 1
    assert text.count("| Original p1 | p1 |") == 1
    assert text.count("| Original p2 | p2 |") == 1
    assert item.papers[0].bibtex.strip() in text and item.papers[0].csl_json.strip() in text
    assert "Original Author" in text and "10.1234/p1" in text
    assert "缺少可核验标识符，需人工核对" in text and "Original p2（p2）" in text
    assert "未分组论文" in text and "Original p3（p3）" in text
    assert "预算遗漏论文（未发送给模型）" in text and "omitted" in text
    assert "研究空白候选（仅限本次已读输入集合）" in text
    assert "依据论文：" in text and "保留生成时记录" in text
    assert render_outline(item) == text


def test_all_model_and_source_text_is_escaped_outside_attachment_code_fences():
    hostile = (
        "<script>alert(1)</script>\n# injected | [link](https://evil.test) `code` www.evil.test"
    )
    paper = attachment("p1", hostile)
    item = result(
        topic_id=hostile,
        papers=(paper, attachment("p2", verified=False), attachment("p3")),
        groups=(OutlineGroup(name=hostile, description=hostile, goal_ids=(), paper_ids=("p1",)),),
        gaps=(OutlineGap(description=hostile, paper_ids=("p1",)),),
        ungrouped=("p2", "p3", "omitted"),
        warnings=(hostile,),
    )
    visible = outside_fences(render_outline(item))
    assert "<script>" not in visible and "&lt;script&gt;" in visible
    assert "\n# injected" not in visible and "[link](https:" not in visible
    assert "www.evil.test" not in visible and "`code`" not in visible
    assert "&#124;" in visible and "&\\#" not in visible


@pytest.mark.parametrize("runs", ["```", "``````", "` `` ``` ````"])
def test_saved_reference_text_cannot_close_its_code_fence(runs):
    payload = "citation text\n" + runs + "\n# source heading\n<script>source</script>\n"
    first = attachment("p1").model_copy(update={"bibtex": payload, "csl_json": payload})
    text = render_outline(
        result(papers=(first, attachment("p2", verified=False), attachment("p3")))
    )
    assert payload.strip() in text
    visible = outside_fences(text)
    assert "# source heading" not in visible and "<script>source</script>" not in visible


def test_no_groups_or_gaps_still_lists_input_limits_and_verification_needs():
    text = render_outline(result(groups=(), gaps=(), ungrouped=("p1", "p2", "p3", "omitted")))
    assert "未形成有效分组" in text
    assert "无有效覆盖缺口候选；缺少提取内容不作为研究空白依据" in text
    assert "未分组论文" in text and "Original p1（p1）" in text
    assert "需要人工核对" in text or "需人工核对" in text
    assert "预算遗漏论文" in text


def test_group_table_and_references_follow_group_input_order_without_sorting():
    group = OutlineGroup(name="ordered", description="short", goal_ids=(), paper_ids=("p2", "p1"))
    text = render_outline(result(groups=(group,)))
    assert text.index("| Original p2 |") < text.index("| Original p1 |")
    assert text.index("#### Original p2") < text.index("#### Original p1")
