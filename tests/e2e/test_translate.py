"""按需段落翻译（第 12.3 节，ADR 0004）：流式返回、结束后校验、通过才缓存、按论文计费。"""

import pytest
import yaml

from litgleaner.application import translation
from litgleaner.domain.errors import ConfigError, InvalidRequest
from tests.e2e.conftest import Harness, paper

# 假解析器的文档：0 摘要、1 标题、2 “We study the problem.”、3 标题、4 “We found it works.”
BODY = 2


@pytest.fixture
def paper_id(harness: Harness) -> str:
    harness.write_papers([paper("Top Paper", 5)])
    harness.run()
    (paper_id,) = harness.ctx.store.judgments.papers_with_scores()
    return paper_id


def run(harness: Harness, paper_id: str, index: int = BODY):
    events = list(translation.translate_block(harness.ctx, paper_id, index))
    deltas = [e.text for e in events if isinstance(e, translation.TranslationDelta)]
    (result,) = [e for e in events if isinstance(e, translation.BlockTranslation)]
    return deltas, result


def write_glossary(harness: Harness, terms: list[dict]) -> None:
    path = harness.root / "config" / "topics" / "alpha-topic.glossary.yaml"
    path.write_text(yaml.safe_dump({"terms": terms}, allow_unicode=True), encoding="utf-8")
    harness.rebuild()


def test_streams_then_caches_verified_translation(harness: Harness, paper_id: str):
    calls = harness.llm_calls("translate")
    deltas, result = run(harness, paper_id)

    assert len(deltas) >= 2  # 增量返回，不是一次性
    assert result.sources == ("We study the problem.",)
    assert result.sentences == ("【译】We study the problem.",)
    assert (result.verified, result.cached) == (True, False)
    assert harness.llm_calls("translate") == calls + 1
    assert harness.ctx.store.runs.llm_cost_for_paper(paper_id) == pytest.approx(0.001)

    _, again = run(harness, paper_id)
    assert (again.cached, again.sentences) == (True, result.sentences)
    assert harness.llm_calls("translate") == calls + 1  # 同一段只翻一次
    assert (
        translation.cached_translations(harness.ctx, paper_id)[BODY].sentences == result.sentences
    )


def test_missing_glossary_term_is_not_cached(harness: Harness, paper_id: str):
    write_glossary(harness, [{"en": "problem", "zh": "问题"}])
    _, result = run(harness, paper_id)
    assert not result.verified
    assert "问题" in result.problems[0]
    assert BODY not in translation.cached_translations(harness.ctx, paper_id)

    harness.llm.text_overrides["translate"] = "我们研究这个问题。"
    _, fixed = run(harness, paper_id)
    assert fixed.verified and fixed.sentences == ("我们研究这个问题。",)


def test_sentence_count_mismatch_is_flagged(harness: Harness, paper_id: str):
    harness.llm.text_overrides["translate"] = "第一句。\n多出来的一句。"
    _, result = run(harness, paper_id)
    assert not result.verified and "句数" in result.problems[0]


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


def test_without_translate_task_reports_config_error(harness: Harness, paper_id: str):
    harness.edit_yaml("llm.yaml", lambda d: d["tasks"].pop("translate"))
    harness.rebuild()
    with pytest.raises(ConfigError, match="translate"):
        run(harness, paper_id)


def test_model_output_links_are_removed(harness: Harness, paper_id: str):
    harness.llm.text_overrides["translate"] = "详见 https://evil.example/x 的说明。"
    _, result = run(harness, paper_id)
    assert "https://" not in result.sentences[0]
