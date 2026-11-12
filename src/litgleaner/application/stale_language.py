"""译文和问答的零费用核验；只使用冻结事实与当前配置，不补算历史结果。"""

from collections import defaultdict
from dataclasses import asdict, dataclass

from litgleaner.application.qa_context import build_inputs
from litgleaner.application.stale_inputs import StaleInputs
from litgleaner.application.stale_qa_memory import answer_item as memory_answer_item
from litgleaner.application.translation import UNIT, choose_cached, translatable_indices
from litgleaner.application.translation import translator_version as current_translator
from litgleaner.domain.document import Block
from litgleaner.domain.errors import InvalidRequest, PermanentError
from litgleaner.domain.hashing import stable_hash
from litgleaner.domain.language import LanguageCode, language_name
from litgleaner.domain.paragraphs import Paragraph, paragraphs
from litgleaner.domain.qa import Answer, Question
from litgleaner.domain.stale import StaleItem, item
from litgleaner.ports.stale import StaleTranslation
from litgleaner.ports.translations import CachedTranslation, TranslationKey


@dataclass(frozen=True)
class _Paragraph:
    paragraph: Paragraph
    glossary_hash: str


@dataclass
class _Translations:
    sources: dict[tuple[str, str], list[_Paragraph]]
    selected: set[TranslationKey]
    readable: set[str]
    errors: dict[str, str]
    version: str | None


def _paragraph_index(basis: StaleInputs) -> _Translations:
    ctx = basis.ctx
    version = current_translator(ctx) if "translate" in ctx.llm.routes else None
    result = _Translations(defaultdict(list), set(), set(), {}, version)
    caches: dict[str, list[CachedTranslation]] = defaultdict(list)
    for row in sorted(
        basis.snapshot.translations, key=lambda row: row.cache.created_at, reverse=True
    ):
        if (
            row.cache.key.unit == UNIT
            and row.cache.key.target_lang == ctx.settings.reading_language
        ):
            caches[row.cache.key.source_hash].append(row.cache)
    for paper_id in basis.papers:
        try:
            blocks = basis.document(paper_id).blocks
        except ValueError as error:
            result.errors[paper_id] = str(error)
            continue
        result.readable.add(paper_id)
        glossary = basis.glossary(paper_id).glossary_hash
        languages: tuple[LanguageCode, ...] = ("zh-CN", "en")
        for language in languages:
            _index_paragraphs(
                result, blocks, language, glossary, caches, target=ctx.settings.reading_language
            )
    return result


def _index_paragraphs(
    result: _Translations,
    blocks: tuple[Block, ...],
    language: LanguageCode,
    glossary_hash: str,
    caches: dict[str, list[CachedTranslation]],
    *,
    target: LanguageCode,
) -> None:
    for paragraph in paragraphs(blocks, translatable_indices(blocks, language)):
        source_hash = stable_hash(paragraph.text)
        result.sources[language, source_hash].append(_Paragraph(paragraph, glossary_hash))
        if language == target:
            key = (
                TranslationKey(UNIT, source_hash, target, glossary_hash, result.version)
                if result.version is not None
                else None
            )
            chosen = choose_cached(paragraph, caches.get(source_hash, ()), key)
            if chosen is not None:
                result.selected.add(chosen.key)


def _translation_item(basis: StaleInputs, row: StaleTranslation, index: _Translations) -> StaleItem:
    key = row.cache.key
    reasons: list[str] = []
    unchecked: list[str] = []
    if key.unit != UNIT:
        reasons.append("翻译单位已变化")
    if key.target_lang != basis.ctx.settings.reading_language:
        reasons.append("阅读语言已变化")
    if index.version is None:
        unchecked.append("翻译模型未配置，无法核对生产者版本")
    elif key.translator_version != index.version:
        reasons.append("译者版本已变化（提示词、模型、服务配置或翻译规则）")
    matches = index.sources.get((key.target_lang, key.source_hash), ())
    if not matches:
        if row.paper_id in index.readable:
            reasons.append("原文段落或段落划分已变化")
        else:
            unchecked.append(index.errors.get(row.paper_id or "", "无法定位缓存对应的当前原文段落"))
        unchecked.append("无法核对当前段落句数和术语表")
    else:
        valid = [m for m in matches if len(m.paragraph.sentences) == len(row.cache.sentences)]
        if not valid:
            reasons.append("译文句数与当前原文不一致")
        if not any(m.glossary_hash == key.glossary_hash for m in valid or matches):
            reasons.append("术语表已变化")
    selected = key in index.selected if matches or row.paper_id in index.readable else None
    return item(
        "translation",
        stable_hash(asdict(key)),
        row.paper_id,
        None,
        reasons=reasons,
        unchecked=unchecked,
        selected=selected,
    )


def check_translations(basis: StaleInputs) -> tuple[StaleItem, ...]:
    if not basis.snapshot.translations:
        return ()
    index = _paragraph_index(basis)
    return tuple(_translation_item(basis, row, index) for row in basis.snapshot.translations)


def _planned_inputs(
    basis: StaleInputs, question: Question, reasons: list[str], unchecked: list[str]
) -> None:
    payload = basis.snapshot.ask_jobs.get(question.id, {})
    state = basis.snapshot.fulltexts.get(question.paper_id)
    expected = {"chunker": basis.ctx.settings.chunker.version}
    if state is not None and state.doc_key is not None:
        expected["doc_key"] = state.doc_key
    try:
        expected["document_hash"] = stable_hash(basis.document(question.paper_id).to_dict())
    except ValueError as error:
        unchecked.append(str(error))
    labels = {"doc_key": "原文归档", "document_hash": "原文内容", "chunker": "切分规则"}
    for field, label in labels.items():
        if field not in payload:
            unchecked.append(f"历史问答任务未保存{label}指纹")
        elif field in expected and payload[field] != expected[field]:
            reasons.append(f"{label}已变化")


def _answer_item(
    basis: StaleInputs, answer: Answer, question: Question | None, profile: str
) -> StaleItem:
    reasons: list[str] = []
    unchecked = ["历史回答未保存完整模型与服务配置，无法核对生产者"]
    route = basis.ctx.llm.routes.get("ask")
    if route is None:
        unchecked.append("问答模型未配置，无法重建实际输入或核对提示词")
    elif answer.producer_version != route.prompt.version:
        reasons.append("问答提示词已变化")
    if question is None:
        unchecked.append("缺少回答对应的问题记录")
    else:
        if question.paper_id != answer.paper_id:
            reasons.append("回答与问题所属论文不一致")
        if question.reading_language != basis.ctx.settings.reading_language:
            reasons.append("阅读语言已变化")
        _planned_inputs(basis, question, reasons, unchecked)
        if route is not None:
            try:
                system = route.prompt.render(
                    {"output_language": language_name(basis.ctx.settings.reading_language)}
                )
                _, inputs = build_inputs(
                    question,
                    basis.document(question.paper_id),
                    basis.chunks(question.paper_id),
                    profile,
                    system,
                )
                if answer.input_hash != stable_hash(inputs):
                    reasons.append("问答实际输入已变化（问题、选区、启用画像或正文上下文）")
            except ValueError as error:
                unchecked.append(str(error))
            except (InvalidRequest, PermanentError):
                reasons.append("当前选区或问答输入已不再有效")
    return item(
        "answer",
        answer.id,
        answer.paper_id,
        None,
        reasons=reasons,
        unchecked=unchecked,
        selected=True,
    )


def check_answers(basis: StaleInputs) -> tuple[StaleItem, ...]:
    questions = {question.id: question for question in basis.snapshot.questions}
    profile = "\n\n".join(topic.profile_text() for topic in basis.active_topics().values())[:500]
    return tuple(
        memory_answer_item(basis, answer, questions.get(answer.question_id))
        if answer.context
        else _answer_item(basis, answer, questions.get(answer.question_id), profile)
        for answer in basis.snapshot.answers
    )
