"""问答引文必须同时存在于规范来源、单个发送片段和完整清洗消息。"""

import re
from collections.abc import Mapping
from typing import Any

from litgleaner.application.qa_memory_checks import canonical_text, decode_manifest
from litgleaner.application.qa_memory_validation import check_sources
from litgleaner.application.stale_inputs import StaleInputs
from litgleaner.domain.evidence import clean_untrusted, verify_quote
from litgleaner.domain.qa import Citation, NoteCitation, Question, Statement
from litgleaner.domain.qa_memory import QASource

_MENTION = re.compile(r"\[\[paper:([^\]\s]+)\]\]")


def _fallback(question: Question) -> tuple[Statement, ...]:
    text = (
        "当前发送的原文和用户记录不足以核实这次回答，请缩小问题或补充依据。"
        if question.reading_language == "zh-CN"
        else "The supplied passages and user records do not provide enough evidence to verify "
        "this answer. Please narrow the question or provide supporting material."
    )
    return (Statement(text, (), True),)


def _sequence(raw: Any) -> tuple[Any, ...]:
    return tuple(raw) if isinstance(raw, list | tuple) else ()


def _author_citation(
    raw: Any, sources: tuple[QASource, ...], actual: Mapping[str, Any], basis: StaleInputs
) -> Citation | None:
    if not isinstance(raw, Mapping):
        return None
    paper_id, page, quote = raw.get("paper_id"), raw.get("page"), raw.get("quote")
    if (
        not isinstance(paper_id, str)
        or type(page) is not int
        or page < 0
        or not isinstance(quote, str)
    ):
        return None
    for source in sources:
        sent = actual.get(source.id)
        if source.kind != "author" or (source.paper_id, source.page) != (paper_id, page):
            continue
        if not isinstance(sent, Mapping) or not isinstance(sent.get("text"), str):
            continue
        try:
            original = canonical_text(basis, source)
        except ValueError:
            continue
        if (
            isinstance(original, str)
            and verify_quote(quote, original)
            and verify_quote(quote, source.text)
            and verify_quote(quote, sent["text"])
        ):
            return Citation(paper_id, page, quote)
    return None


def _note_citation(
    raw: Any, sources: tuple[QASource, ...], actual: Mapping[str, Any], basis: StaleInputs
) -> NoteCitation | None:
    if not isinstance(raw, Mapping):
        return None
    paper_id, id_, quote = raw.get("paper_id"), raw.get("source_id"), raw.get("quote")
    kind = raw.get("kind", "note")
    if not isinstance(paper_id, str) or not isinstance(id_, str) or not isinstance(quote, str):
        return None
    if not paper_id or not id_ or not quote:
        return None
    if not isinstance(kind, str) or kind not in {"note", "question"}:
        return None
    for source in sources:
        sent = actual.get(source.id)
        if (source.paper_id, source.source_id, source.kind) != (paper_id, id_, kind):
            continue
        if not isinstance(sent, Mapping) or not isinstance(sent.get("text"), str):
            continue
        original = canonical_text(basis, source)
        if (
            isinstance(original, str)
            and verify_quote(quote, original)
            and verify_quote(quote, source.text)
            and verify_quote(quote, sent["text"])
        ):
            return NoteCitation(paper_id, id_, quote, "note" if kind == "note" else "question")
    return None


def _mentions(
    raw: Mapping[str, Any],
    text: str,
    question: Question,
    citations: tuple[Citation, ...],
    notes: tuple[NoteCitation, ...],
    *,
    titles: Mapping[str, str],
) -> tuple[str, ...] | None:
    declared = raw.get("mentioned_paper_ids", [])
    if not isinstance(declared, list | tuple) or any(not isinstance(id_, str) for id_ in declared):
        return None
    mentioned = set(declared) | set(_MENTION.findall(text))
    for key in ("citations", "note_citations"):
        for citation in _sequence(raw.get(key)):
            if isinstance(citation, Mapping) and isinstance(citation.get("paper_id"), str):
                mentioned.add(citation["paper_id"])
    if not mentioned <= titles.keys():
        return None
    covered = {item.paper_id for item in citations} | {item.paper_id for item in notes}
    external = mentioned - {question.paper_id}
    if not external <= covered:
        return None
    normalized = " ".join(text.split()).casefold()
    for paper_id, title in titles.items():
        if paper_id == question.paper_id or paper_id in mentioned:
            continue
        identity = re.search(r"(?<!\w)" + re.escape(paper_id) + r"(?!\w)", text)
        named = " ".join(clean_untrusted(title).split()).casefold()
        if identity or (named and named in normalized):
            return None
    return tuple(sorted(external))


def _statement(
    raw: Any,
    question: Question,
    sources: tuple[QASource, ...],
    actual: Mapping[str, Any],
    titles: Mapping[str, str],
    *,
    basis: StaleInputs,
) -> Statement | None:
    if (
        not isinstance(raw, Mapping)
        or not isinstance(raw.get("text"), str)
        or not raw["text"].strip()
    ):
        return None
    if any(
        key in raw and not isinstance(raw[key], list | tuple)
        for key in ("citations", "note_citations")
    ):
        return None
    citations = tuple(
        dict.fromkeys(
            citation
            for item in _sequence(raw.get("citations"))
            if (citation := _author_citation(item, sources, actual, basis)) is not None
        )
    )
    notes = tuple(
        dict.fromkeys(
            note
            for item in _sequence(raw.get("note_citations"))
            if (note := _note_citation(item, sources, actual, basis)) is not None
        )
    )
    mentions = _mentions(raw, raw["text"], question, citations, notes, titles=titles)
    if mentions is None:
        return None
    return Statement(raw["text"], citations, not (citations or notes), notes, mentions)


def verified_statements(
    question: Question, manifest: Mapping[str, Any], basis: StaleInputs, raw_statements: Any
) -> tuple[Statement, ...]:
    reasons, unchecked = check_sources(basis, question, manifest)
    if reasons or unchecked:
        return _fallback(question)
    sources, inputs = decode_manifest(manifest)
    actual = {source["id"]: source for source in inputs["sources"]}
    results = tuple(
        result
        for raw in _sequence(raw_statements)
        if (result := _statement(raw, question, sources, actual, manifest["papers"], basis=basis))
        is not None
    )
    return results or _fallback(question)
