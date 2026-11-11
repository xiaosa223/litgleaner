"""一次快照后的索引和文档缓存；核验期间不再查询数据库。"""

from collections.abc import Mapping

from litgleaner.application.context import AppContext
from litgleaner.application.documents import load_document
from litgleaner.application.translation import merged_glossary
from litgleaner.domain.chunker import ChunkedDocument, chunk_document
from litgleaner.domain.document import NormalizedDocument
from litgleaner.domain.glossary import Glossary
from litgleaner.domain.paper import Paper
from litgleaner.domain.topic import Topic
from litgleaner.ports.export import TopicExportSnapshot
from litgleaner.ports.stale import StaleAssessment, StaleSnapshot
from litgleaner.ports.store import Extraction, ScoredAssessment


def _validate_document(document: NormalizedDocument) -> None:
    """归档反序列化只检查文字和页码；这里核对后续切分与段落索引的必要结构。"""
    if (
        not isinstance(document.source_pdf_sha256, str)
        or not isinstance(document.parser_id, str)
        or (document.title is not None and not isinstance(document.title, str))
        or any(not isinstance(value, str) for value in document.front_matter)
        or any(
            not isinstance(block.kind, str)
            or type(block.page) is not int
            or block.page < 0
            or (block.caption is not None and not isinstance(block.caption, str))
            for block in document.blocks
        )
    ):
        raise ValueError("文档归档结构无效")


class StaleInputs:
    def __init__(self, ctx: AppContext, snapshot: StaleSnapshot) -> None:
        self.ctx, self.snapshot = ctx, snapshot
        self.papers: Mapping[str, Paper] = {paper.id: paper for paper in snapshot.papers}
        self.extractions: dict[str, Extraction] = {}
        for record in sorted(snapshot.extractions, key=lambda record: record.id):
            self.extractions[record.paper_id] = record
        self.assessments: dict[int, StaleAssessment] = {
            record.record.id: record for record in snapshot.assessments
        }
        self._documents: dict[str, NormalizedDocument | ValueError] = {}
        self._chunks: dict[str, ChunkedDocument] = {}
        self._exports: dict[str, TopicExportSnapshot] = {}
        self._latest: dict[str, dict[str, StaleAssessment]] = {}
        self._glossaries: dict[str, Glossary] = {}

    def active_topics(self) -> dict[str, Topic]:
        return {
            state.topic_id: self.ctx.topics[state.topic_id]
            for state in self.snapshot.topic_states
            if state.status == "active" and state.topic_id in self.ctx.topics
        }

    def glossary(self, paper_id: str) -> Glossary:
        if paper_id not in self._glossaries:
            hits = sorted(
                {
                    (row.topic_id, row.topic_hash)
                    for row in self.snapshot.triages
                    if row.paper_id == paper_id and row.relevant
                }
            )
            self._glossaries[paper_id] = merged_glossary(
                self.ctx.settings.reading_language, self.active_topics(), hits, self.ctx.glossaries
            )
        return self._glossaries[paper_id]

    def document(self, paper_id: str) -> NormalizedDocument:
        state = self.snapshot.fulltexts.get(paper_id)
        if state is None or state.doc_key is None:
            raise ValueError("缺少当前文档归档")
        key = state.doc_key
        if key not in self._documents:
            try:
                document = load_document(self.ctx, key)
                _validate_document(document)
                self._documents[key] = document
            except Exception:
                self._documents[key] = ValueError("当前文档归档缺失或损坏，无法完整核验")
        value = self._documents[key]
        if isinstance(value, ValueError):
            raise value
        return value

    def chunks(self, paper_id: str) -> ChunkedDocument:
        document = self.document(paper_id)
        state = self.snapshot.fulltexts[paper_id]
        assert state.doc_key is not None
        if state.doc_key not in self._chunks:
            self._chunks[state.doc_key] = chunk_document(document, self.ctx.settings.chunker)
        return self._chunks[state.doc_key]

    def latest_assessments(self, topic: Topic) -> dict[str, StaleAssessment]:
        if topic.id in self._latest:
            return self._latest[topic.id]
        semantic = self.ctx.rubrics[topic.rubric].semantic_hash
        candidates = sorted(self.snapshot.assessments, key=lambda entry: entry.record.id)
        result = {
            entry.record.paper_id: entry
            for entry in candidates
            if (
                entry.record.topic_id == topic.id
                and entry.record.topic_hash == topic.topic_hash
                and entry.record.semantic_hash == semantic
            )
        }
        self._latest[topic.id] = result
        return result

    def topic_snapshot(self, topic: Topic) -> TopicExportSnapshot:
        if topic.id in self._exports:
            return self._exports[topic.id]
        ids = {
            row.paper_id
            for row in self.snapshot.triages
            if (row.topic_id == topic.id and row.topic_hash == topic.topic_hash and row.relevant)
        }
        latest = self.latest_assessments(topic)
        policy = self.ctx.rubrics[topic.rubric].policy_hash
        scores: dict[str, ScoredAssessment] = {
            score.assessment.paper_id: score
            for score in self.snapshot.scores
            if (
                score.assessment.paper_id in latest
                and score.policy_hash == policy
                and score.assessment.id == latest[score.assessment.paper_id].record.id
            )
        }
        result = TopicExportSnapshot(
            tuple(paper for paper in self.snapshot.papers if paper.id in ids),
            self.snapshot.aliases,
            self.extractions,
            scores,
            self.snapshot.fulltexts,
            self.snapshot.reading,
        )
        self._exports[topic.id] = result
        return result
