"""问答在排队时冻结完整有界消息，实际发送记忆才参与后续失效核验。"""

import json
from collections.abc import Mapping
from dataclasses import asdict, dataclass, field
from typing import Any

from litgleaner.application.context import AppContext
from litgleaner.application.qa_context import TOKEN_BUDGET, qa_token_count, validate_selection
from litgleaner.application.qa_memory_retrieval import QARetrieval, collect
from litgleaner.application.qa_schema import ANSWER_SCHEMA
from litgleaner.application.stale_inputs import StaleInputs
from litgleaner.domain.errors import InvalidRequest, NotFound
from litgleaner.domain.evidence import clean_untrusted, wrap_untrusted
from litgleaner.domain.hashing import stable_hash
from litgleaner.domain.language import language_name
from litgleaner.domain.qa import Question
from litgleaner.domain.qa_memory import CONTRACT, QASource


def producer_version(ctx: AppContext) -> str | None:
    route = ctx.llm.routes.get("ask")
    return (
        None
        if route is None
        else stable_hash(
            {
                "contract": CONTRACT,
                "schema": ANSWER_SCHEMA,
                "prompt": route.prompt.version,
                "model": route.model,
                "provider": route.provider_version,
            }
        )
    )


def research_profile(basis: StaleInputs) -> str:
    return "\n\n".join(topic.profile_text() for topic in basis.active_topics().values())[:500]


@dataclass
class _Builder:
    question: Question
    basis: StaleInputs
    system: str
    profile: str
    sources: list[QASource] = field(default_factory=list)
    relations: list[dict[str, Any]] = field(default_factory=list)
    backgrounds: dict[str, Mapping[str, Any]] = field(default_factory=dict)

    def data(
        self,
        sources: list[QASource],
        relations: list[dict[str, Any]],
        backgrounds: Mapping[str, Mapping[str, Any]],
    ) -> dict[str, Any]:
        ids = {self.question.paper_id, *(source.paper_id for source in sources)}
        return {
            "paper_id": self.question.paper_id,
            "question": self.question.text,
            "selection": self.question.selection,
            "research_profile": self.profile,
            "papers": {id_: self.basis.papers[id_].title for id_ in sorted(ids)},
            "sources": [asdict(source) for source in sources],
            "relations": [
                {key: r[key] for key in ("id", "paper_a", "paper_b", "kind", "explanation")}
                for r in relations
            ],
            "model_backgrounds": {
                id_: dict(data.get("fields", {})) for id_, data in backgrounds.items()
            },
        }

    def fits(
        self,
        sources: list[QASource],
        relations: list[dict[str, Any]],
        backgrounds: Mapping[str, Mapping[str, Any]],
        *,
        limit: int = TOKEN_BUDGET,
    ) -> bool:
        text = clean_untrusted(
            json.dumps(
                self.data(sources, relations, backgrounds), ensure_ascii=False, sort_keys=True
            )
        )
        return qa_token_count(self.system + wrap_untrusted(text)) <= limit

    def add(self, source: QASource, *, limit: int = TOKEN_BUDGET) -> bool:
        if source.id in {item.id for item in self.sources}:
            return True
        if not self.fits([*self.sources, source], self.relations, self.backgrounds, limit=limit):
            return False
        self.sources.append(source)
        return True


def _main(builder: _Builder, found: QARetrieval) -> None:
    question, basis = builder.question, builder.basis
    if question.selection is not None:
        source = QASource(
            f"selection:{question.paper_id}",
            question.paper_id,
            "author",
            question.selection,
            question.page,
            basis.snapshot.fulltexts[question.paper_id].doc_key,
        )
        if not builder.add(source):
            raise InvalidRequest("问答输入过长，无法容纳选区，请缩小范围后重新提问")
    memory = found.relations or any(
        s.paper_id != question.paper_id or s.kind != "author" for s in found.sources
    )
    limit = TOKEN_BUDGET - 1000 if memory else TOKEN_BUDGET
    for source in found.sources:
        if (
            source.paper_id == question.paper_id
            and source.kind == "author"
            and not source.id.startswith("relation:")
        ):
            builder.add(source, limit=limit)


def _memory(builder: _Builder, found: QARetrieval) -> None:
    by_id = {source.id: source for source in found.sources}
    for relation in found.relations:
        ids = (
            f"relation:{relation.id}:{relation.paper_a}",
            f"relation:{relation.id}:{relation.paper_b}",
        )
        if not all(id_ in by_id for id_ in ids):
            continue
        added = [by_id[id_] for id_ in ids if by_id[id_] not in builder.sources]
        records = [*builder.relations, relation.model_dump(mode="json")]
        if builder.fits([*builder.sources, *added], records, builder.backgrounds):
            builder.sources.extend(added)
            builder.relations = records
    candidates = sorted(
        found.sources, key=lambda s: s.paper_id == builder.question.paper_id and s.kind == "author"
    )
    for source in candidates:
        if not source.id.startswith("relation:"):
            builder.add(source)
    sent_ids = {source.paper_id for source in builder.sources}
    for id_, record in sorted(found.backgrounds.items()):
        if id_ in sent_ids:
            backgrounds = {**builder.backgrounds, id_: record}
            if builder.fits(builder.sources, builder.relations, backgrounds):
                builder.backgrounds = backgrounds


def prepare(ctx: AppContext, question: Question) -> dict[str, Any]:
    basis = StaleInputs(ctx, ctx.store.stale_snapshot())
    if question.paper_id not in basis.papers:
        raise NotFound(f"论文不存在：{question.paper_id}")
    try:
        document = basis.document(question.paper_id)
    except ValueError as error:
        raise InvalidRequest("这篇论文还没有可读取的解析正文，无法问答") from error
    validate_selection(document, question.selection, question.page)
    route = ctx.llm.require_route("ask")
    system = route.prompt.render({"output_language": language_name(question.reading_language)})
    builder = _Builder(question, basis, system, research_profile(basis))
    if not builder.fits([], [], {}):
        raise InvalidRequest("问答输入过长，请缩小问题或系统提示后重新提问")
    found = collect(ctx, question, basis)
    _main(builder, found)
    _memory(builder, found)
    data = builder.data(builder.sources, builder.relations, builder.backgrounds)
    text = clean_untrusted(json.dumps(data, ensure_ascii=False, sort_keys=True))
    return {
        "contract": CONTRACT,
        "inputs": text,
        "system": system,
        "producer": producer_version(ctx),
        "prompt": route.prompt.version,
        "reading_language": question.reading_language,
        "question_hash": stable_hash(asdict(question)),
        "profile_hash": stable_hash(builder.profile),
        "chunker": ctx.settings.chunker.version,
        "document_hash": stable_hash(document.to_dict()),
        "doc_key": basis.snapshot.fulltexts[question.paper_id].doc_key,
        "sources": [asdict(source) for source in builder.sources],
        "relations": builder.relations,
        "backgrounds": builder.backgrounds,
        "papers": data["papers"],
        "warnings": list(found.warnings),
    }
