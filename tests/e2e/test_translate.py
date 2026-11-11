"""按需翻译（第 12.3 节，ADR 0004）。

按段落、按句编号分批、占位符保护、逐段校验、通过才缓存、按论文计费。
"""

import pytest
import yaml

from litgleaner.application import translation
from litgleaner.domain.errors import InvalidRequest, ModelConfigurationRequired, TransientError
from litgleaner.ports.llm import StreamDone, TextDelta, Thinking
from tests.e2e.conftest import Harness, paper

# 假解析器的文档：0 “Abstract—Study of <标题>.”、1 标题、2 “We study the problem.”、
# 3 标题、4 “We found it works.”
BODY = 2
OTHER = 4


@pytest.fixture
def paper_id(harness: Harness) -> str:
    return _paper(harness, "Top Paper")


def _paper(harness: Harness, title: str) -> str:
    harness.write_papers([paper(title, 5)])
    harness.run()
    (paper_id,) = harness.ctx.store.judgments.papers_with_scores()
    return paper_id


def run(harness: Harness, paper_id: str, index: int = BODY):
    events = list(translation.translate_block(harness.ctx, paper_id, index))
    deltas = [e.text for e in events if isinstance(e, translation.TranslationDelta)]
    (result,) = [e for e in events if isinstance(e, translation.BlockTranslation)]
    return deltas, result


def run_many(harness: Harness, paper_id: str, indices: list[int]):
    return list(translation.translate_blocks(harness.ctx, paper_id, indices))


def blocks(events) -> dict[int, translation.BlockTranslation]:
    return {e.index: e for e in events if isinstance(e, translation.BlockTranslation)}


def write_glossary(harness: Harness, terms: list[dict]) -> None:
    path = harness.root / "config" / "topics" / "alpha-topic.glossary.yaml"
    path.write_text(yaml.safe_dump({"terms": terms}, allow_unicode=True), encoding="utf-8")
    harness.rebuild()


def test_streams_sentence_then_caches_verified_translation(harness: Harness, paper_id: str):
    calls = harness.llm_calls("translate")
    deltas, result = run(harness, paper_id)

    assert deltas == ["【译】We study the problem."]  # 每句到齐就先显示，不等校验
    assert result.sources == ("We study the problem.",)
    assert result.sentences == ("【译】We study the problem.",)
    assert (result.verified, result.cached) == (True, False)
    assert harness.llm_calls("translate") == calls + 1
    assert harness.ctx.store.runs.llm_cost_for_paper(paper_id) == pytest.approx(0.001)
    assert "[[1]] We study the problem." in harness.llm.text_calls[-1].user

    _, again = run(harness, paper_id)
    assert (again.cached, again.sentences) == (True, result.sentences)
    assert harness.llm_calls("translate") == calls + 1  # 同一段只翻一次
    assert (
        translation.cached_translations(harness.ctx, paper_id)[BODY].sentences == result.sentences
    )


def test_page_translation_batches_paragraphs_into_one_call(harness: Harness, paper_id: str):
    calls = harness.llm_calls("translate")
    events = run_many(harness, paper_id, [0, 1, BODY, 3, OTHER])

    found = blocks(events)
    assert sorted(found) == [0, BODY, OTHER]  # 标题不翻译
    assert all(b.verified for b in found.values())
    assert harness.llm_calls("translate") == calls + 1  # 三段一批
    sent = harness.llm.text_calls[-1].user
    assert "[[2]] We study the problem." in sent and "[[3]] We found it works." in sent

    again = run_many(harness, paper_id, [BODY, OTHER])
    assert all(b.cached for b in blocks(again).values())
    assert harness.llm_calls("translate") == calls + 1


def test_fragile_tokens_are_protected_and_restored(harness: Harness):
    paper_id = _paper(harness, "Model in Figure 3")
    _, result = run(harness, paper_id, index=0)

    sent = harness.llm.text_calls[-1].user
    assert "⟦0⟧" in sent and "Figure 3" not in sent
    assert result.verified
    assert result.sentences == ("【译】Abstract—Study of Model in Figure 3.",)


def test_dropped_placeholder_keeps_original_text(harness: Harness):
    paper_id = _paper(harness, "Model in Figure 3")
    harness.llm.text_overrides["translate"] = "[[1]] 模型研究。"
    _, result = run(harness, paper_id, index=0)
    assert not result.verified and result.sentences == ()  # 不用残缺的译文覆盖原文
    assert "占位符" in result.problems[0]


def test_broken_paragraph_in_batch_is_retried_alone(harness: Harness, paper_id: str):
    harness.llm.text_overrides["translate"] = "[[1]] 摘要。\n[[2]] 我们研究这个问题。"
    calls = harness.llm_calls("translate")
    events = run_many(harness, paper_id, [0, BODY, OTHER])

    assert harness.llm_calls("translate") == calls + 2  # 整批一次，缺句的那段单独再翻一次
    other = [e for e in events if isinstance(e, translation.BlockTranslation) and e.index == OTHER]
    assert [(e.verified, e.sentences) for e in other] == [(False, ()), (True, ("摘要。",))]
    assert "[[1]] We found it works." in harness.llm.text_calls[-1].user  # 重试只送这一段


def test_missing_glossary_term_is_not_cached(harness: Harness, paper_id: str):
    write_glossary(harness, [{"en": "problem", "zh": "问题"}])
    _, result = run(harness, paper_id)
    assert not result.verified
    assert result.sentences  # 只是译名不对：照常显示，标为未通过
    assert "问题" in result.problems[0]
    assert BODY not in translation.cached_translations(harness.ctx, paper_id)

    harness.llm.text_overrides["translate"] = "[[1]] 我们研究这个问题。"
    _, fixed = run(harness, paper_id)
    assert fixed.verified and fixed.sentences == ("我们研究这个问题。",)


def test_unnumbered_output_aligns_by_position_only_for_one_paragraph(
    harness: Harness, paper_id: str
):
    harness.llm.text_overrides["translate"] = "我们研究这个问题。"
    _, result = run(harness, paper_id)
    assert result.verified and result.sentences == ("我们研究这个问题。",)

    harness.llm.text_overrides["translate"] = "第一句。\n多出来的一句。"
    _, result = run(harness, paper_id, index=OTHER)
    assert not result.verified and "句数" in result.problems[0]


def test_thinking_model_reports_status_before_text(harness: Harness, paper_id: str):
    def thinking_stream(request):
        yield Thinking()
        yield TextDelta("[[1]] 我们研究这个问题。")
        yield StreamDone(request.model, 10, 5, 0.0)

    harness.llm.stream = thinking_stream
    events = list(translation.translate_block(harness.ctx, paper_id, BODY))
    assert isinstance(events[0], translation.TranslationStatus)
    assert events[0].state == "thinking"


def test_glossary_change_marks_old_translation_stale_without_retranslating(
    harness: Harness, paper_id: str
):
    run(harness, paper_id)
    calls = harness.llm_calls("translate")
    write_glossary(harness, [{"en": "works", "zh": "有效"}])

    cached = translation.cached_translations(harness.ctx, paper_id)[BODY]
    assert cached.stale  # 术语表变了：旧译文照常显示，标为旧版本
    assert harness.llm_calls("translate") == calls  # 不自动重译


def test_heading_and_unknown_block_are_rejected(harness: Harness, paper_id: str):
    with pytest.raises(InvalidRequest):
        run(harness, paper_id, index=1)
    with pytest.raises(InvalidRequest):
        run(harness, paper_id, index=99)
    with pytest.raises(InvalidRequest):
        run_many(harness, paper_id, [1, 3])


def test_without_translate_task_reports_configuration_required(harness: Harness, paper_id: str):
    harness.edit_yaml("llm.yaml", lambda d: d["tasks"].pop("translate"))
    harness.rebuild()
    with pytest.raises(ModelConfigurationRequired, match="translate"):
        run(harness, paper_id)


def test_model_output_links_are_removed(harness: Harness, paper_id: str):
    harness.llm.text_overrides["translate"] = "[[1]] 详见 https://evil.example/x 的说明。"
    _, result = run(harness, paper_id)
    assert "https://" not in result.sentences[0]


def test_out_of_order_lines_still_settle_each_paragraph(harness: Harness, paper_id: str):
    harness.llm.text_overrides["translate"] = (
        "[[3]] 我们发现有效。\n[[1]] 摘要。\n[[2]] 我们研究问题。"
    )
    calls = harness.llm_calls("translate")
    found = blocks(run_many(harness, paper_id, [0, BODY, OTHER]))
    assert all(b.verified for b in found.values())
    assert harness.llm_calls("translate") == calls + 1  # 不因乱序重试


def test_leading_number_in_translation_is_kept(harness: Harness, paper_id: str):
    harness.llm.text_overrides["translate"] = "[[1]] 2. 5 倍的提升。"
    _, result = run(harness, paper_id)
    assert result.sentences == ("2. 5 倍的提升。",)


def test_overlong_line_fails_instead_of_truncating(harness: Harness, paper_id: str):
    harness.llm.text_overrides["translate"] = "[[1]] " + "长" * 5000
    _, result = run(harness, paper_id)
    assert not result.verified and result.sentences == ()


def test_truncated_batch_keeps_finished_paragraphs_and_retries_the_rest(
    harness: Harness, paper_id: str
):
    original = harness.llm.stream
    attempts = []

    def flaky(request):
        attempts.append(request.user)
        if len(attempts) == 1:  # 整批：第一段写完后被截断
            yield TextDelta("[[1]] 摘要。\n[[2]] 我们研")
            yield StreamDone(request.model, 10, 5, 0.0, error=TransientError("输出被截断"))
            raise TransientError("输出被截断")
        yield from original(request)

    harness.llm.stream = flaky
    events = run_many(harness, paper_id, [0, BODY, OTHER])

    found = blocks(events)
    assert all(b.verified for b in found.values())
    assert found[0].sentences == ("摘要。",)  # 截断前完成的段落保留，不重翻
    assert len(attempts) == 3  # 整批一次，剩下两段各一次
    assert "We study" in attempts[1] and "We found" in attempts[2]
