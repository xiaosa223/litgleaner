"""问答记忆证据验收的真实归档、规范记录与冻结清单构造。"""

import json
from dataclasses import asdict

from litgleaner.application.relate_input import source_hash
from litgleaner.application.stale_inputs import StaleInputs
from litgleaner.domain.evidence import clean_untrusted
from litgleaner.domain.hashing import stable_hash
from litgleaner.domain.paper_relation import PaperRelation, RelationEvidence
from litgleaner.domain.qa import Question
from litgleaner.domain.qa_memory import CONTRACT, QASource
from litgleaner.domain.reading import NewAnnotation
from tests.e2e.memory_helpers import add
from tests.e2e.relate_helpers import HERE, THERE, source


def _setup(harness, *, left=HERE, right=THERE):
    a, b = add(harness, "qa-main"), add(harness, "qa-memory")
    source(harness, a, left)
    source(harness, b, right)
    question = Question(
        "question-current",
        a,
        "request",
        "Explain the evidence.",
        None,
        2,
        "zh-CN",
        harness.ctx.now_iso(),
    )
    return a, b, question, StaleInputs(harness.ctx, harness.ctx.store.stale_snapshot())


def _author(basis, paper_id, text, *, id_=None):
    return QASource(
        id_ or f"author:{paper_id}:{stable_hash(text)}",
        paper_id,
        "author",
        text,
        2,
        basis.snapshot.fulltexts[paper_id].doc_key,
    )


def _manifest(basis, question, sources, *, relations=(), backgrounds=None):
    backgrounds = backgrounds or {}
    ids = {question.paper_id, *(row.paper_id for row in sources), *backgrounds}
    for relation in relations:
        ids.update((relation.paper_a, relation.paper_b))
    papers = {id_: basis.papers[id_].title for id_ in ids}
    manifest = {
        "contract": CONTRACT,
        "sources": [asdict(row) for row in sources],
        "relations": [row.model_dump(mode="json") for row in relations],
        "backgrounds": backgrounds,
        "papers": papers,
        "system": "Use the supplied original evidence.",
        "producer": "producer",
        "prompt": "prompt",
        "reading_language": question.reading_language,
        "question_hash": stable_hash(asdict(question)),
        "profile_hash": stable_hash("profile"),
        "chunker": basis.ctx.settings.chunker.version,
        "document_hash": stable_hash(basis.document(question.paper_id).to_dict()),
    }
    manifest["inputs"] = clean_untrusted(
        json.dumps(
            {
                "sources": manifest["sources"],
                "papers": papers,
                "relations": [
                    {key: row[key] for key in ("id", "paper_a", "paper_b", "kind", "explanation")}
                    for row in manifest["relations"]
                ],
                "model_backgrounds": {id_: row["fields"] for id_, row in backgrounds.items()},
            }
        )
    )
    return manifest


def _raw(
    text="This statement follows the supplied original passage.",
    *,
    citations=(),
    notes=(),
    mentions=(),
):
    return {
        "text": text,
        "citations": list(citations),
        "note_citations": list(notes),
        "mentioned_paper_ids": list(mentions),
    }


def _note(harness, paper_id, body):
    return harness.ctx.store.reader.add_annotation(
        NewAnnotation(paper_id, "note", None, None, None, body), harness.ctx.now_iso()
    )


def _note_source(note):
    return QASource(
        "note:" + note.id,
        note.paper_id,
        "note",
        note.body,
        note.page,
        source_id=note.id,
        version=stable_hash(asdict(note)),
    )


def _relation(basis, a, b):
    states = basis.snapshot.fulltexts
    return PaperRelation(
        id="relation-current",
        batch_id="user-batch",
        paper_a=a,
        paper_b=b,
        kind="extends",
        evidence_a=RelationEvidence(paper_id=a, doc_key=states[a].doc_key, page=2, quote=HERE),
        evidence_b=RelationEvidence(paper_id=b, doc_key=states[b].doc_key, page=2, quote=THERE),
        explanation="The original passages support this relationship.",
        source_hashes={
            id_: source_hash(basis.papers[id_], states[id_], basis.extractions[id_])
            for id_ in (a, b)
        },
        input_hash="input",
        producer_version="producer",
        created_at=basis.ctx.now_iso(),
        source="user",
        feedback_id="feedback",
    )
