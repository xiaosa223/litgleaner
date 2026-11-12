"""文库事实关系的批量只读投影，不排队或补算历史结果。"""

from collections import defaultdict
from collections.abc import Mapping
from typing import Any

from litgleaner.application.context import AppContext
from litgleaner.application.stages.extract import (
    extraction_input_hash,
    extraction_producer_version,
)
from litgleaner.domain.errors import InvalidRequest, NotFound
from litgleaner.domain.evidence import MIN_QUOTE_CHARS
from litgleaner.domain.knowledge import Concept, ConceptOccurrence, normalize_concept_name
from litgleaner.domain.memory import (
    FactSignals,
    FieldEvidence,
    MemoryFacts,
    MemoryPaper,
    MemoryWarning,
)
from litgleaner.domain.memory_citations import citation_index
from litgleaner.domain.memory_projection import project_facts
from litgleaner.ports.memory import MemorySnapshot
from litgleaner.ports.store import Extraction


def _input_problem(ctx: AppContext, data: MemorySnapshot, extraction: Extraction) -> str | None:
    state = data.fulltexts.get(extraction.paper_id)
    if state is None or state.doc_key is None:
        return "missing_document"
    expected = extraction_input_hash(
        state.doc_key,
        ctx.settings.chunker,
        ctx.settings.reading_language,
        ctx.settings.assess_token_budget,
    )
    return "stale_input" if extraction.input_hash != expected else None


def _fields(raw: list[Any], extraction: Extraction) -> tuple[FieldEvidence, ...]:
    found: list[FieldEvidence] = []
    for item in raw:
        if not isinstance(item, Mapping) or item.get("verified") is not True:
            continue
        text, quote, page = item.get("text"), item.get("quote"), item.get("page")
        if (
            isinstance(text, str)
            and text.strip()
            and isinstance(quote, str)
            and len(" ".join(quote.split())) >= MIN_QUOTE_CHARS
            and type(page) is int
            and page >= 0
        ):
            found.append(
                FieldEvidence(
                    text,
                    quote,
                    page,
                    extraction.id,
                    extraction.input_hash,
                    extraction.extractor_version,
                )
            )
    return tuple(dict.fromkeys(found))


_DETAILS = {
    "missing_extraction": "尚无提取，只参与引文关系查询",
    "missing_document": "提取缺少当前文档指针，未使用其概念、数据集和方法",
    "stale_input": "提取输入与当前文档、切分、语言或预算不符，未使用其事实",
    "stale_producer": "提取生产者与当前配置不符，未使用其事实",
    "producer_unchecked": "未配置提取模型，已有提取的输入可核对，生产者未完整核验",
    "legacy_fields": "提取缺少部分结构化字段，缺失部分未参与匹配",
    "stale_concept": "部分核心概念位置不对应最新提取的指纹、角色或名称，未参与匹配",
}


def _signals(
    ctx: AppContext, data: MemorySnapshot
) -> tuple[dict[str, FactSignals], tuple[MemoryWarning, ...]]:
    producer = extraction_producer_version(ctx)
    extractions = {item.paper_id: item for item in data.extractions}
    concepts = {item.id: item for item in data.concepts}
    occurrences: dict[str, list[ConceptOccurrence]] = defaultdict(list)
    for item in data.occurrences:
        if item.role == "core":
            occurrences[item.paper_id].append(item)
    signals: dict[str, FactSignals] = {}
    warnings: list[MemoryWarning] = []
    for paper in data.papers:
        extraction = extractions.get(paper.id)
        if extraction is None:
            codes = ["missing_extraction"]
        else:
            problem = _input_problem(ctx, data, extraction)
            if (
                problem is None
                and producer is not None
                and extraction.extractor_version != producer
            ):
                problem = "stale_producer"
            if problem is not None:
                codes = [problem]
            else:
                signal, codes = _usable_signals(
                    extraction, concepts, data.concept_names, occurrences[paper.id]
                )
                signals[paper.id] = signal
                if producer is None:
                    codes.append("producer_unchecked")
        warnings.extend(MemoryWarning(paper.id, code, _DETAILS[code]) for code in codes)
    return signals, tuple(warnings)


def _core_names(raw: Any) -> set[tuple[str, int]]:
    if not isinstance(raw, list):
        return set()
    return {
        (normalize_concept_name(item["name"]), item["page"])
        for item in raw
        if isinstance(item, Mapping)
        and item.get("role") == "core"
        and isinstance(item.get("name"), str)
        and type(item.get("page")) is int
    }


def _usable_signals(
    extraction: Extraction,
    concepts: Mapping[str, Concept],
    concept_names: Mapping[str, tuple[str, ...]],
    occurrences: list[ConceptOccurrence],
) -> tuple[FactSignals, list[str]]:
    data = extraction.data
    datasets, method = data.get("datasets"), data.get("method")
    complete = (
        isinstance(data.get("key_concepts"), list)
        and isinstance(datasets, list)
        and "method" in data
        and (method is None or isinstance(method, Mapping))
    )
    codes = [] if complete else ["legacy_fields"]
    names = _core_names(data.get("key_concepts"))
    core = []
    for item in occurrences:
        concept = concepts.get(item.concept_id)
        registered = concept_names.get(item.concept_id, ())
        if concept is not None:
            registered = (*registered, normalize_concept_name(concept.name))
        if (
            item.input_hash != extraction.input_hash
            or item.producer_version != extraction.extractor_version
            or concept is None
            or not any((name, item.page) in names for name in registered)
        ):
            if "stale_concept" not in codes:
                codes.append("stale_concept")
        else:
            core.append((concept, item))
    return FactSignals(
        tuple(core),
        _fields(datasets if isinstance(datasets, list) else [], extraction),
        _fields([method] if isinstance(method, Mapping) else [], extraction),
    ), codes


def facts(ctx: AppContext, paper_id: str, *, limit: int = 50, offset: int = 0) -> MemoryFacts:
    if not 1 <= limit <= 200 or offset < 0:
        raise InvalidRequest("论文事实关系查询参数越界")
    data = ctx.store.memory_snapshot()
    papers = {
        paper.id: MemoryPaper(
            paper.id,
            paper.title,
            data.reading[paper.id].status if paper.id in data.reading else "new",
        )
        for paper in data.papers
    }
    if paper_id not in papers:
        raise NotFound(f"论文尚未进入文库或不存在：{paper_id}")
    index = citation_index(data.citations, data.aliases, {p.id: p.title for p in data.papers})
    signals, warnings = _signals(ctx, data)
    matches = project_facts(paper_id, papers, index, signals)
    return MemoryFacts(
        papers[paper_id], matches[offset : offset + limit], len(matches), limit, offset, warnings
    )
