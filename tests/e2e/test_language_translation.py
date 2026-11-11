"""翻译语言、术语约束与执行确认：改语言只让缓存失效，不自动产生模型费用。"""

import pytest
import yaml
from alembic import command

from litgleaner.application import reader, translation, translation_plan
from litgleaner.application.documents import save_document
from litgleaner.application.pipeline import run_on_demand
from litgleaner.application.stages.discover import resolve_record
from litgleaner.domain.document import Block, NormalizedDocument
from litgleaner.domain.hashing import stable_hash
from litgleaner.domain.paper import SourceRecord
from litgleaner.infra.config import load_config
from litgleaner.infra.db.migrate import alembic_config, upgrade_database
from litgleaner.ports.store import FullTextState
from litgleaner.ports.translations import TranslationKey
from tests.e2e.conftest import Harness


@pytest.fixture
def paper_id(harness: Harness) -> str:
    paper_id = resolve_record(
        harness.ctx.store.papers, SourceRecord(title="A bilingual study"), harness.ctx.now_iso()
    )
    doc = NormalizedDocument(1, "a" * 64, "test", None, (
        Block("heading", "Abstract", 0),
        Block("text", "We study the learning problem.", 0),
        Block("text", "我们研究模型的学习能力。", 1),
        Block("text", "Transformer 模型的学习能力需要进一步研究。", 2),
    ), (), "ok")  # fmt: skip
    key, _ = save_document(harness.ctx, doc)
    harness.ctx.store.papers.save_fulltext(
        FullTextState(paper_id, "parsed", doc_key=key), harness.ctx.now_iso()
    )
    return paper_id


def _reading(h: Harness, language: str) -> None:
    h.edit_yaml("app.yaml", lambda data: data["language"].update(reading=language))
    h.rebuild()


def test_reader_flags_and_translator_agree_on_the_target_language(harness: Harness, paper_id: str):
    view = reader.document_view(harness.ctx, paper_id)
    assert [b.index for b in view.blocks if b.translatable] == [1, 3]
    _reading(harness, "en")
    view = reader.document_view(harness.ctx, paper_id)
    assert [b.index for b in view.blocks if b.translatable] == [2, 3]
    assert [p.indices for p in translation.paper_paragraphs(harness.ctx, paper_id)]
    with pytest.raises(Exception, match="不需要翻译"):
        list(translation.translate_block(harness.ctx, paper_id, 1))


def test_english_output_prompt_and_cache_are_separate_from_chinese(harness: Harness, paper_id: str):
    list(translation.translate_block(harness.ctx, paper_id, 3))
    calls = harness.llm_calls("translate")
    assert 3 in translation.cached_translations(harness.ctx, paper_id)
    _reading(harness, "en")
    assert translation.cached_translations(harness.ctx, paper_id) == {}
    assert harness.llm_calls("translate") == calls
    list(translation.translate_block(harness.ctx, paper_id, 3))
    assert "译成 English" in harness.llm.text_calls[-1].system
    assert "{{target_language}}" not in harness.llm.text_calls[-1].system
    paragraph = next(
        p for p in translation.paper_paragraphs(harness.ctx, paper_id) if 3 in p.indices
    )
    key = translation.cache_key(
        harness.ctx, paragraph, translation.paper_glossary(harness.ctx, paper_id)
    )
    assert key.target_lang == "en"
    assert harness.ctx.store.translations.get(key) is not None


def test_chinese_glossary_is_not_applied_to_english_output(harness: Harness, paper_id: str):
    path = harness.root / "config/topics/alpha-topic.glossary.yaml"
    path.write_text(
        yaml.safe_dump({"terms": [{"en": "Transformer", "zh": "变换器"}]}), encoding="utf-8"
    )
    _reading(harness, "en")
    assert translation.paper_glossary(harness.ctx, paper_id).terms == ()
    assert list(translation.translate_block(harness.ctx, paper_id, 3))[-1].verified


def test_chinese_source_terms_are_enforced_in_english_translation(harness: Harness, paper_id: str):
    path = harness.root / "config/topics/alpha-topic.glossary.yaml"
    path.write_text(
        yaml.safe_dump(
            {
                "target_lang": "en",
                "terms": [{"forms": "学习能力", "translation": "learning ability"}],
            }
        ),
        encoding="utf-8",
    )
    _reading(harness, "en")
    failed = list(translation.translate_block(harness.ctx, paper_id, 2))[-1]
    assert not failed.verified
    assert "learning ability" in harness.llm.text_calls[-1].system
    harness.llm.text_overrides["translate"] = "[[1]] We study the learning ability of the model."
    passed = list(translation.translate_block(harness.ctx, paper_id, 2))[-1]
    assert passed.verified


def test_old_confirmation_is_not_executed_after_language_change(harness: Harness, paper_id: str):
    translation_plan.queue(harness.ctx, paper_id)
    _reading(harness, "en")
    run_on_demand(harness.ctx)
    assert harness.llm_calls("translate") == 0
    assert len(harness.ctx.store.jobs.list_by_status("dead")) == 1
    assert translation_plan.plan(harness.ctx, paper_id).state == "idle"


def test_provider_change_invalidates_old_confirmation(harness: Harness, paper_id: str):
    translation_plan.queue(harness.ctx, paper_id)

    def switch(data):
        data["providers"]["other"] = {"adapter": "fake_llm"}
        data["tasks"]["translate"]["provider"] = "other"

    harness.edit_yaml("llm.yaml", switch)
    harness.rebuild()
    run_on_demand(harness.ctx)
    assert harness.llm_calls("translate") == 0
    assert len(harness.ctx.store.jobs.list_by_status("dead")) == 1


def test_migrated_legacy_chinese_cache_is_still_shown_without_new_calls(
    harness: Harness, paper_id: str
):
    paragraph = next(
        p for p in translation.paper_paragraphs(harness.ctx, paper_id) if 3 in p.indices
    )
    key = TranslationKey(
        "block", stable_hash(paragraph.text), "zh", "old-glossary", "old-translator"
    )
    harness.ctx.store.translations.put(
        key, ("旧的中文译文。",), "legacy-model", paper_id, harness.ctx.now_iso()
    )
    url = load_config(harness.root / "config").database_url
    command.downgrade(alembic_config(url), "0008")
    upgrade_database(url)
    found = translation.cached_translations(harness.ctx, paper_id)[3]
    assert found.sentences == ("旧的中文译文。",) and found.stale
    assert harness.llm_calls("translate") == 0
