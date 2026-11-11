"""课题对比表保留证据来源、未知值及两种格式的文本边界。"""

import csv
import io
from dataclasses import FrozenInstanceError, replace

import pytest

from litgleaner.domain.comparison_table import ComparisonRow, render_table
from litgleaner.domain.goal_links import GoalLink


def evidence(text="作者使用混合方法", quote="We use a hybrid method.", page=0, verified=True):
    return {"text": text, "quote": quote, "page": page, "verified": verified}


def row(**changes):
    result = ComparisonRow(
        paper_id="p-1",
        title="Original title",
        extraction={
            "method": evidence(),
            "datasets": [evidence("Dataset A", "We evaluate on Dataset A.", 1)],
            "metrics": [evidence("Accuracy", "We report accuracy on the held-out set.", 2)],
            "results": [
                evidence("Accuracy 92%, 10 trials", "Accuracy reaches 92% over 10 trials.", 2)
            ],
            "code_data": [
                {
                    **evidence("代码仓库", "We release the code in our repository.", 3),
                    "kind": "code",
                    "availability": "public",
                    "url": "https://example.test/ExactPath",
                    "url_verified": True,
                }
            ],
        },
        extraction_stale=False,
        reading_value=81,
        tier="must_read",
        score_stale=False,
        provisional=False,
        goal_links=(
            GoalLink(
                goal_id="g1",
                reuse="用于方法比较",
                quote="Our method is efficient.",
                page=4,
                verified=True,
            ),
        ),
        goals={"g1": "研究目标一"},
    )
    return replace(result, **changes)


def csv_rows(rows):
    return list(csv.reader(io.StringIO(render_table(tuple(rows), "csv"))))


def cells(item):
    headers, values = csv_rows([item])
    return dict(zip(headers, values, strict=True))


def test_comparison_row_is_frozen_and_public_formats_are_explicit():
    with pytest.raises(FrozenInstanceError):
        row().title = "changed"
    with pytest.raises(ValueError, match="format"):
        render_table((row(),), "html")


@pytest.mark.parametrize("format", ["md", "csv"])
def test_complete_row_preserves_evidence_and_assessment_sources(format):
    text = render_table((row(),), format)
    assert "Original title" in text and "p-1" in text
    assert "模型概括" in text and "原文引用" in text
    assert "作者使用混合方法" in text and "We use a hybrid method." in text
    assert "PDF 第 1 页" in text and "PDF 第 3 页" in text
    assert "Accuracy 92%, 10 trials" in text
    assert "已核验" in text and "81" in text
    assert (r"must\_read" if format == "md" else "must_read") in text
    assert "评估关联" in text and "模型推荐理由" in text
    assert "研究目标一" in text and "用于方法比较" in text
    assert "Our method is efficient." in text and "PDF 第 5 页" in text


def test_missing_old_fields_and_explicit_absence_are_different_and_no_publication_is_guessed():
    old = cells(row(extraction={"summary": "old summary", "innovation": "old innovation"}))
    empty = cells(
        row(
            extraction={
                "method": None,
                "datasets": [],
                "metrics": [],
                "results": [],
                "code_data": [],
            }
        )
    )
    for name in ("方法", "数据集", "代码/数据公开情况"):
        assert old[name] == "未提取"
        assert "本次提取未获得可核验证据" in empty[name]
    assert "未提取" in old["指标与结果"]
    assert "old summary" not in str(old) and "old innovation" not in str(old)
    assert "不公开" not in empty["代码/数据公开情况"]
    assert cells(row(extraction=None))["方法"] == "未提取"


@pytest.mark.parametrize("flag", [False, None, 1, "true", "false"])
def test_evidence_does_not_coerce_verification_flags(flag):
    item = evidence(verified=flag)
    if flag is None:
        del item["verified"]
    text = cells(row(extraction={"method": item}))["方法"]
    assert "未核验" in text and "已核验" not in text
    assert "We use a hybrid method." in text


@pytest.mark.parametrize("page", [None, -1, True, "0"])
def test_bad_or_missing_pages_are_not_displayed_as_real_pdf_pages(page):
    text = cells(row(extraction={"method": evidence(page=page)}))["方法"]
    assert "页码未记录" in text
    assert "PDF 第" not in text


@pytest.mark.parametrize("flag", [False, None, 1, "true"])
def test_unverified_code_urls_are_not_exported_as_links(flag):
    data = {
        "code_data": [
            {
                **evidence(),
                "kind": "code",
                "availability": "public",
                "url": "https://unapproved.test/private-path",
                "url_verified": flag,
            }
        ]
    }
    text = cells(row(extraction=data))["代码/数据公开情况"]
    assert "unapproved.test" not in text
    assert "链接未核验或未提供" in text


@pytest.mark.parametrize(
    "url",
    [
        "javascript:alert(1)",
        "file:///private/file",
        "https://user:password@example.test/code",
        "https://example.test/space here",
        "https://example.test/\ncode",
        "https://example.test/%0Acode",
        "https://bad_host.test/code",
        "https://example.test:99999/code",
        "https://example.test\\code",
        "https:///missing-host",
        "https://-bad.test/code",
        "https://example.test/<script>",
    ],
)
def test_even_verified_url_flags_do_not_bypass_url_syntax_checks(url):
    data = {
        "code_data": [
            {
                **evidence(),
                "kind": "code",
                "availability": "public",
                "url": url,
                "url_verified": True,
            }
        ]
    }
    text = cells(row(extraction=data))["代码/数据公开情况"]
    assert url not in text and "链接未核验或未提供" in text


@pytest.mark.parametrize(
    "availability,label", [("public", "公开"), ("not_public", "不公开"), ("unknown", "未知")]
)
def test_code_and_data_availability_are_model_records_with_explicit_verification(
    availability, label
):
    data = {
        "code_data": [
            {
                **evidence(verified=False),
                "kind": "data",
                "availability": availability,
                "url": None,
                "url_verified": False,
            }
        ]
    }
    text = cells(row(extraction=data))["代码/数据公开情况"]
    assert "数据" in text and label in text and "模型记录" in text and "未核验" in text


def test_scores_show_absence_staleness_and_provisional_status_without_recomputing():
    assert cells(row(reading_value=None, tier=None))["阅读价值分"] == "未评分"
    stale = cells(row(reading_value=None, tier=None, score_stale=True))["阅读价值分"]
    assert "未评分" in stale and "过期" in stale
    provisional = cells(row(provisional=True))["阅读价值分"]
    assert "81" in provisional and "暂定分" in provisional
    state = cells(row(extraction_stale=True, score_stale=True))["状态"]
    assert "提取已过期" in state and "评分已过期" in state
    assert "过期" in cells(row(score_stale=True))["阅读价值分"]


def test_goal_links_preserve_assessment_reasons_and_unknown_goal_identity():
    link = GoalLink(
        goal_id="unknown",
        reuse="评估给出的复用建议",
        quote="Original goal support.",
        page=0,
        verified=False,
    )
    data = cells(row(goal_links=(link,), goals={}))
    text = data["对应目标"]
    assert "评估关联" in text and "未知目标" in text and "unknown" in text
    assert "模型推荐理由：评估给出的复用建议" in text
    assert "原文引用（未核验）" in text and "Original goal support." in text
    assert "PDF 第 1 页" in text
    assert "Original goal support." not in data["方法"]
    assert cells(row(goal_links=()))["对应目标"] == "未记录目标关联"


@pytest.mark.parametrize("flag", [False, None, 1, "true"])
def test_goal_link_verification_is_not_coerced_by_the_renderer(flag):
    link = GoalLink.model_construct(
        goal_id="g1", reuse="评估建议", quote="Original goal support.", page=0, verified=flag
    )
    text = cells(row(goal_links=(link,)))["对应目标"]
    assert "原文引用（未核验）" in text and "已核验" not in text


def test_markdown_escapes_source_markup_and_newlines_without_extra_columns_or_active_links():
    source = (
        "<script>alert(1)</script> | `code`\n[click](javascript:alert(2)) *bold* \\ www.bad.test"
    )
    data = {"method": evidence(text=source, quote=source)}
    text = render_table((row(title=source, paper_id="id|injected", extraction=data),), "md")
    lines = text.splitlines()
    assert len(lines) == 3
    assert all(len(re_split(line)) == 9 for line in lines)
    assert "<script>" not in text and "&lt;script&gt;" in text
    assert "[click](javascript:" not in text and "`code`" not in text
    assert "<br>" in text
    assert "javascript:" not in text
    assert "www.bad.test" not in text
    assert "&#124;" in text and "&#58;" in text and "www&#46;" in text
    assert "&\\#" not in text


def re_split(line):
    return line.strip().strip("|").split(" | ")


@pytest.mark.parametrize(
    "value",
    [
        "=SUM(A1:A2)",
        "+run",
        "-run",
        "@run",
        "  =run",
        "\x00\x1b+run",
        "\ttext",
        "\rtext",
    ],
)
def test_csv_prevents_formula_injection_even_after_leading_whitespace_or_control(value):
    data = csv_rows([row(title=value, paper_id=value)])
    assert len(data) == 2 and len(data[1]) == len(data[0]) == 9
    assert data[1][0] == "'" + value and data[1][1] == "'" + value


def test_csv_roundtrip_preserves_commas_quotes_newlines_html_and_literal_evidence():
    source = 'Original "quoted", <b>text</b>\nwith a second line | `literal`'
    item = row(title=source, extraction={"method": evidence(text=source, quote=source)})
    exported = csv_rows([item])
    assert len(exported) == 2 and len(exported[1]) == 9
    assert exported[1][0] == source
    assert source in exported[1][2]
    assert "模型概括" in exported[1][2] and "原文引用" in exported[1][2]


@pytest.mark.parametrize("format", ["md", "csv"])
def test_empty_tables_keep_headers_and_row_order_is_exactly_input_order(format):
    text = render_table((), format)
    assert "论文标题" in text and "阅读价值分" in text
    first, second = row(paper_id="z", title="First input"), row(paper_id="a", title="Second input")
    result = render_table((first, second), format)
    assert result.index("First input") < result.index("Second input")
    assert result == render_table((first, second), format)
