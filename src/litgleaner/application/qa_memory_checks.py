"""问答冻结来源的结构与规范文本；只使用已有快照和共享文档缓存。"""

import json
from collections.abc import Mapping
from dataclasses import asdict
from typing import Any

from litgleaner.application.stale_inputs import StaleInputs
from litgleaner.domain.evidence import clean_untrusted
from litgleaner.domain.hashing import stable_hash
from litgleaner.domain.paper_relation import PaperRelation
from litgleaner.domain.qa import Question
from litgleaner.domain.qa_memory import CONTRACT, QASource


def contains(fragment: str, text: str) -> bool:
    needle = " ".join(fragment.split()).casefold()
    return bool(needle) and needle in " ".join(text.split()).casefold()


def _source(raw: Any) -> QASource:
    if not isinstance(raw, Mapping) or set(raw) != {
        "id",
        "paper_id",
        "kind",
        "text",
        "page",
        "doc_key",
        "source_id",
        "version",
    }:
        raise ValueError("问答缺少完整冻结来源")
    if any(
        not isinstance(raw[key], str) or not raw[key].strip()
        for key in ("id", "paper_id", "kind", "text")
    ):
        raise ValueError("问答冻结来源的身份或文本无效")
    if raw["kind"] not in {"author", "note", "question"} or not isinstance(raw["version"], str):
        raise ValueError("问答冻结来源类型或版本无效")
    page = raw["page"]
    if page is not None and (type(page) is not int or page < 0):
        raise ValueError("问答冻结来源页码无效")
    if raw["kind"] == "author":
        valid = page is not None and isinstance(raw["doc_key"], str) and raw["doc_key"].strip()
        if not valid or raw["source_id"] is not None or raw["version"] != "":
            raise ValueError("问答作者原文来源结构无效")
    elif (
        raw["doc_key"] is not None or not isinstance(raw["source_id"], str) or not raw["source_id"]
    ):
        raise ValueError("问答用户来源结构无效")
    return QASource(**raw)


def decode_manifest(manifest: Mapping[str, Any]) -> tuple[tuple[QASource, ...], dict[str, Any]]:
    if manifest.get("contract") != CONTRACT:
        raise ValueError("问答缺少可核对的记忆协议")
    for key in (
        "inputs",
        "system",
        "producer",
        "prompt",
        "reading_language",
        "question_hash",
        "profile_hash",
        "chunker",
        "document_hash",
    ):
        if not isinstance(manifest.get(key), str) or not manifest[key]:
            raise ValueError("问答缺少完整冻结输入与生产者")
    raw = manifest.get("sources")
    if not isinstance(raw, list):
        raise ValueError("问答缺少完整冻结来源")
    sources = tuple(_source(item) for item in raw)
    if len({item.id for item in sources}) != len(sources):
        raise ValueError("问答冻结来源编号重复")
    try:
        inputs = json.loads(manifest["inputs"])
    except (TypeError, ValueError) as error:
        raise ValueError("问答完整冻结消息无法解码") from error
    if not isinstance(inputs, dict):
        raise ValueError("问答完整冻结消息结构无效")
    return sources, inputs


def actual_sources(sources: tuple[QASource, ...]) -> list[dict[str, Any]]:
    decoded: list[dict[str, Any]] = json.loads(
        clean_untrusted(json.dumps([asdict(source) for source in sources]))
    )
    return decoded


def user_record(basis: StaleInputs, source: QASource) -> Any:
    records = basis.snapshot.annotations if source.kind == "note" else basis.snapshot.questions
    return next((record for record in records if record.id == source.source_id), None)


def canonical_text(basis: StaleInputs, source: QASource) -> str | None:
    if source.kind == "author":
        document = basis.document(source.paper_id)
        return "\n".join(
            "\n".join(filter(None, (block.caption, block.text)))
            if source.id.startswith("relation:")
            else block.text
            for block in document.blocks
            if block.page == source.page
        )
    record = user_record(basis, source)
    if record is None:
        return None
    text = record.body if source.kind == "note" else record.text
    return text if isinstance(text, str) else None


def _author_problem(
    basis: StaleInputs, source: QASource, relations: Mapping[str, PaperRelation], question: Question
) -> tuple[str | None, str | None]:
    state = basis.snapshot.fulltexts.get(source.paper_id)
    if state is None or state.doc_key != source.doc_key:
        return "问答已发送原文的文档指针已变化或缺失", None
    try:
        document = basis.document(source.paper_id)
    except ValueError as error:
        return None, str(error)
    texts: tuple[str, ...]
    if source.id.startswith("selection:"):
        if (source.id, source.paper_id, source.page, source.text) != (
            f"selection:{question.paper_id}",
            question.paper_id,
            question.page,
            question.selection,
        ):
            return "问答选区来源与冻结问题的原文或页码不符", None
        texts = ("\n".join(block.text for block in document.blocks if block.page == source.page),)
    elif source.id.startswith("relation:"):
        if not _relation_fragment(source, relations):
            return "问答关系原文与冻结关系身份或证据不符", None
        texts = (canonical_text(basis, source) or "",)
    else:
        texts = tuple(block.text for block in document.blocks if block.page == source.page)
    if not any(contains(source.text, text) for text in texts):
        return "问答已发送原文片段不再匹配指定论文的原页", None
    return None, None


def _relation_fragment(source: QASource, relations: Mapping[str, PaperRelation]) -> bool:
    expected = next(
        (
            evidence
            for relation in relations.values()
            for evidence in (relation.evidence_a, relation.evidence_b)
            if source.id == f"relation:{relation.id}:{evidence.paper_id}"
        ),
        None,
    )
    return expected is not None and (source.paper_id, source.doc_key, source.page, source.text) == (
        expected.paper_id,
        expected.doc_key,
        expected.page,
        expected.quote,
    )


def source_problem(
    basis: StaleInputs, question: Question, source: QASource, relations: Mapping[str, PaperRelation]
) -> tuple[str | None, str | None]:
    if source.paper_id not in basis.papers:
        return "问答已发送来源论文不存在", None
    if (
        source.paper_id != question.paper_id
        and source.paper_id not in basis.snapshot.library_paper_ids
    ):
        return "问答已发送来源论文已退出文库", None
    if source.kind == "author":
        return _author_problem(basis, source, relations, question)
    return _user_problem(basis, question, source)


def _user_problem(
    basis: StaleInputs, question: Question, source: QASource
) -> tuple[str | None, str | None]:
    record = user_record(basis, source)
    if record is None or record.paper_id != source.paper_id:
        return "问答已发送用户来源已删除或身份不符", None
    if source.kind == "question" and record.id == question.id:
        return "问答用户来源不是历史问题", None
    if source.version != stable_hash(asdict(record)) or source.page != record.page:
        return "问答已发送用户来源的版本或页码已变化", None
    text = record.body if source.kind == "note" else record.text
    if not isinstance(text, str) or not contains(source.text, text):
        return "问答已发送片段不在规范用户正文中", None
    return None, None
