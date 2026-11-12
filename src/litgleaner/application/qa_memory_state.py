"""冻结问答的配置及规范源核验；只读报告与付费调用共用同一合同。"""

import json
from collections.abc import Mapping
from dataclasses import asdict
from typing import Any

from litgleaner.application.qa_context import TOKEN_BUDGET, qa_token_count
from litgleaner.application.qa_memory_input import producer_version, research_profile
from litgleaner.application.qa_memory_validation import check_sources
from litgleaner.application.relate_input import configuration
from litgleaner.application.stages.extract import extraction_producer_version
from litgleaner.application.stale_inputs import StaleInputs
from litgleaner.domain.evidence import clean_untrusted, wrap_untrusted
from litgleaner.domain.hashing import stable_hash
from litgleaner.domain.language import language_name
from litgleaner.domain.qa import Question
from litgleaner.domain.qa_memory import CONTRACT


def check(
    basis: StaleInputs, question: Question, manifest: Mapping[str, Any]
) -> tuple[tuple[str, ...], tuple[str, ...]]:
    reasons, unchecked = [], []
    if manifest.get("contract") != CONTRACT:
        unchecked.append("缺少可核对的问答记忆协议")
    if manifest.get("question_hash") != stable_hash(asdict(question)):
        reasons.append("问题或选区已经变化")
    if question.reading_language != basis.ctx.settings.reading_language:
        reasons.append("阅读语言已变化")
    if manifest.get("chunker") != basis.ctx.settings.chunker.version:
        reasons.append("切分规则已变化")
    state = basis.snapshot.fulltexts.get(question.paper_id)
    if state is not None and manifest.get("doc_key") != state.doc_key:
        reasons.append("原文归档已变化")
    if manifest.get("profile_hash") != stable_hash(research_profile(basis)):
        reasons.append("研究画像已变化")
    try:
        if manifest.get("document_hash") != stable_hash(
            basis.document(question.paper_id).to_dict()
        ):
            reasons.append("原文内容已变化")
    except ValueError as error:
        unchecked.append(str(error))
    if not _actual_inputs(basis, question, manifest):
        reasons.append("实际问答消息与规范问题、选区或画像不符")
    _producer(basis, question, manifest, reasons, unchecked)
    source_reasons, source_unchecked = check_sources(basis, question, manifest)
    return tuple(dict.fromkeys((*reasons, *source_reasons))), tuple(
        dict.fromkeys((*unchecked, *source_unchecked))
    )


def _actual_inputs(basis: StaleInputs, question: Question, manifest: Mapping[str, Any]) -> bool:
    expected = {
        "paper_id": question.paper_id,
        "question": question.text,
        "selection": question.selection,
        "research_profile": research_profile(basis),
    }
    try:
        actual = json.loads(manifest.get("inputs", ""))
        clean = json.loads(clean_untrusted(json.dumps(expected, ensure_ascii=False)))
    except (TypeError, ValueError):
        return False
    return isinstance(actual, dict) and all(
        actual.get(key) == value for key, value in clean.items()
    )


def _producer(
    basis: StaleInputs,
    question: Question,
    manifest: Mapping[str, Any],
    reasons: list[str],
    unchecked: list[str],
) -> None:
    route = basis.ctx.llm.routes.get("ask")
    if route is None:
        unchecked.append("问答模型未配置，无法核对完整请求生产者")
    else:
        if manifest.get("producer") != producer_version(basis.ctx):
            reasons.append("问答提示词、请求模型或服务配置已变化")
        system = route.prompt.render({"output_language": language_name(question.reading_language)})
        if qa_token_count(system + wrap_untrusted(str(manifest.get("inputs", "")))) > TOKEN_BUDGET:
            reasons.append("当前选区或问答输入已不再有效（输入过长）")
        elif manifest.get("system") != system:
            reasons.append("问答实际系统提示已变化")


def _metadata_scope(manifest: Mapping[str, Any]) -> tuple[set[str], set[str]]:
    main_id = json.loads(manifest["inputs"])["paper_id"]
    endpoints = {
        relation[key]
        for relation in manifest.get("relations", [])
        for key in ("paper_a", "paper_b")
    }
    authors = {source["paper_id"] for source in manifest["sources"] if source["kind"] == "author"}
    library = (set(manifest["papers"]) - {main_id}) | endpoints
    documents = {main_id} | authors | set(manifest["backgrounds"]) | endpoints
    return library, documents


def state_token(basis: StaleInputs, manifest: Mapping[str, Any]) -> str:
    """最终短写锁只核对实际使用的规范行；文档读取留在锁外。"""
    sources = manifest.get("sources", [])
    ids = set(manifest.get("papers", {}))
    library_ids, document_ids = _metadata_scope(manifest)
    note_ids = {s.get("source_id") for s in sources if s.get("kind") == "note"}
    question_ids = {s.get("source_id") for s in sources if s.get("kind") == "question"}
    relation_ids = {r["id"] for r in manifest.get("relations", [])}
    extraction_ids = set(manifest.get("backgrounds", {}))
    for relation in manifest.get("relations", []):
        extraction_ids.update(relation.get("source_hashes", {}))
    return stable_hash(
        {
            "configuration": {
                "producer": producer_version(basis.ctx),
                "profile": research_profile(basis),
                "language": basis.ctx.settings.reading_language,
                "chunker": basis.ctx.settings.chunker.version,
                "extract_producer": extraction_producer_version(basis.ctx)
                if extraction_ids
                else None,
                "extract_budget": basis.ctx.settings.assess_token_budget
                if extraction_ids
                else None,
                "relations": dict(configuration(basis.ctx)) if relation_ids else None,
            },
            "papers": {id_: basis.papers[id_].title for id_ in ids if id_ in basis.papers},
            "library": sorted(library_ids & basis.snapshot.library_paper_ids),
            "fulltexts": {
                id_: asdict(basis.snapshot.fulltexts[id_])
                for id_ in document_ids
                if id_ in basis.snapshot.fulltexts
            },
            "notes": [asdict(note) for note in basis.snapshot.annotations if note.id in note_ids],
            "questions": [asdict(q) for q in basis.snapshot.questions if q.id in question_ids],
            "relations": [
                r.model_dump(mode="json") for r in basis.snapshot.relations if r.id in relation_ids
            ],
            "extractions": {
                id_: asdict(basis.extractions[id_])
                for id_ in extraction_ids
                if id_ in basis.extractions
            },
        }
    )
