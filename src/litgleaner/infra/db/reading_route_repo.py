"""阅读路线固定四次读取，不按每篇论文重复查询评分、全文和 incoming。"""

from typing import Any

from sqlalchemy import Select, and_, func, select, text

from litgleaner.infra.db.database import Database
from litgleaner.infra.db.judgments_repo import _SCORED_COLUMNS, _to_scored
from litgleaner.infra.db.papers_repo import _to_paper
from litgleaner.infra.db.schema import assessment, fulltext, paper, score, triage
from litgleaner.ports.reading_route import RouteSnapshot
from litgleaner.ports.store import FullTextState, ScoredAssessment

_CITATIONS = """
WITH relevant AS (
    SELECT paper_id FROM triage
    WHERE topic_id=:topic AND topic_hash=:hash AND relevant=1
), matched AS (
    SELECT c.citing_paper_id,c.position,max(a.paper_id) AS cited_paper_id
    FROM citation c
    JOIN relevant r ON r.paper_id=c.citing_paper_id
    JOIN json_each(c.cited_aliases) AS item
    JOIN paper_alias a ON a.scheme=json_extract(item.value,'$.scheme')
        AND a.value=json_extract(item.value,'$.value')
    WHERE c.cited_paper_id IS NULL
    GROUP BY c.citing_paper_id,c.position
    HAVING count(DISTINCT a.paper_id)=1
), edges AS (
    SELECT c.citing_paper_id,c.cited_paper_id FROM citation c
    JOIN relevant r ON r.paper_id=c.citing_paper_id
    WHERE c.cited_paper_id IS NOT NULL
    UNION ALL
    SELECT citing_paper_id,cited_paper_id FROM matched
)
SELECT e.cited_paper_id AS paper_id,count(DISTINCT e.citing_paper_id) AS n
FROM edges e JOIN relevant r ON r.paper_id=e.cited_paper_id
WHERE e.citing_paper_id!=e.cited_paper_id
GROUP BY e.cited_paper_id
"""


def _relevant(topic_id: str, topic_hash: str) -> Select[Any]:
    return select(triage.c.paper_id).where(
        triage.c.topic_id == topic_id,
        triage.c.topic_hash == topic_hash,
        triage.c.relevant.is_(True),
    )


class SqlReadingRouteRepo:
    def __init__(self, db: Database) -> None:
        self._db = db

    def snapshot(
        self, topic_id: str, topic_hash: str, semantic_hash: str, policy_hash: str
    ) -> RouteSnapshot:
        candidates = _relevant(topic_id, topic_hash)
        papers = tuple(
            _to_paper(row)
            for row in self._db.fetch(
                select(paper).where(paper.c.id.in_(candidates)).order_by(paper.c.id)
            )
        )
        if not papers:
            return RouteSnapshot((), {}, {}, {})
        scores = self._scores(candidates, topic_id, topic_hash, semantic_hash, policy_hash)
        fulltexts = self._fulltexts(candidates)
        citations = {
            row.paper_id: int(row.n)
            for row in self._db.fetch(text(_CITATIONS).bindparams(topic=topic_id, hash=topic_hash))
        }
        return RouteSnapshot(papers, scores, fulltexts, citations)

    def _scores(
        self,
        candidates: Select[Any],
        topic_id: str,
        topic_hash: str,
        semantic_hash: str,
        policy_hash: str,
    ) -> dict[str, ScoredAssessment]:
        latest = (
            select(func.max(assessment.c.id).label("id"))
            .where(
                assessment.c.paper_id.in_(candidates),
                assessment.c.topic_id == topic_id,
                assessment.c.topic_hash == topic_hash,
                assessment.c.semantic_hash == semantic_hash,
            )
            .group_by(assessment.c.paper_id)
            .subquery()
        )
        statement = (
            select(*_SCORED_COLUMNS)
            .select_from(assessment)
            .join(latest, assessment.c.id == latest.c.id)
            .outerjoin(
                score,
                and_(score.c.assessment_id == assessment.c.id, score.c.policy_hash == policy_hash),
            )
        )
        return {
            row.paper_id: _to_scored(row)
            for row in self._db.fetch(statement)
            if row.policy_hash is not None
        }

    def _fulltexts(self, candidates: Select[Any]) -> dict[str, FullTextState]:
        return {
            row.paper_id: FullTextState(
                paper_id=row.paper_id,
                status=row.status,
                pdf_sha256=row.pdf_sha256,
                doc_key=row.doc_key,
                provider=row.provider,
                license_basis=row.license_basis,
                quality=row.quality,
            )
            for row in self._db.fetch(select(fulltext).where(fulltext.c.paper_id.in_(candidates)))
        }
