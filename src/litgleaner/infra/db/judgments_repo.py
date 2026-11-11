"""判断层：主题状态、初筛、评估和分数。"""

import json
from typing import Any

from sqlalchemy import Row, func, or_, select
from sqlalchemy.dialects.sqlite import insert

from litgleaner.domain.goal_links import GoalLink
from litgleaner.domain.rubric import ScoreResult
from litgleaner.infra.db.database import Database
from litgleaner.infra.db.schema import (
    assessment,
    digest,
    digest_item,
    fulltext,
    paper,
    rubric_snapshot,
    score,
    topic_state,
    triage,
)
from litgleaner.ports.store import (
    Assessment,
    AssessmentKey,
    NewAssessment,
    RankQuery,
    ScoredAssessment,
    TopicState,
    TriageResult,
)


def _to_assessment(row: Row[Any]) -> Assessment:
    return Assessment(
        id=row.id,
        paper_id=row.paper_id,
        topic_id=row.topic_id,
        topic_hash=row.topic_hash,
        semantic_hash=row.semantic_hash,
        input_hash=row.input_hash,
        levels=json.loads(row.levels),
        evidence=json.loads(row.evidence),
        fulltext_used=bool(row.fulltext_used),
        goal_links=tuple(GoalLink.model_validate(item) for item in json.loads(row.goal_links)),
    )


def _to_scored(row: Row[Any]) -> ScoredAssessment:
    return ScoredAssessment(
        assessment=_to_assessment(row),
        policy_hash=row.policy_hash,
        score=row.score,
        tier=row.tier,
        provisional=bool(row.provisional),
        undetermined=tuple(json.loads(row.undetermined)),
    )


_SCORED_COLUMNS = (
    assessment,
    score.c.policy_hash,
    score.c.score,
    score.c.tier,
    score.c.provisional,
    score.c.undetermined,
)


class SqlJudgmentRepo:
    def __init__(self, db: Database) -> None:
        self._db = db

    def get_topic_state(self, topic_id: str) -> TopicState | None:
        rows = self._db.fetch(select(topic_state).where(topic_state.c.topic_id == topic_id))
        if not rows:
            return None
        row = rows[0]
        return TopicState(row.topic_id, row.status, row.topic_hash, row.triage_from)

    def save_topic_state(self, state: TopicState, now: str) -> None:
        values = {
            "status": state.status,
            "topic_hash": state.topic_hash,
            "triage_from": state.triage_from,
            "updated_at": now,
        }
        self._db.write(
            insert(topic_state)
            .values(topic_id=state.topic_id, **values)
            .on_conflict_do_update(index_elements=["topic_id"], set_=values)
        )

    def topic_states(self) -> tuple[TopicState, ...]:
        rows = self._db.fetch(select(topic_state).order_by(topic_state.c.topic_id))
        return tuple(TopicState(r.topic_id, r.status, r.topic_hash, r.triage_from) for r in rows)

    def _triaged(self, topic_id: str, topic_hash: str) -> Any:
        return select(triage.c.paper_id).where(
            triage.c.topic_id == topic_id, triage.c.topic_hash == topic_hash
        )

    def papers_awaiting_triage(
        self, topic_id: str, topic_hash: str, seen_from: str
    ) -> tuple[str, ...]:
        rows = self._db.fetch(
            select(paper.c.id)
            .where(
                paper.c.first_seen_at >= seen_from,
                paper.c.id.not_in(self._triaged(topic_id, topic_hash)),
            )
            .order_by(paper.c.id)
        )
        return tuple(row.id for row in rows)

    def untriaged_papers(
        self, topic_id: str, topic_hash: str, since: str | None
    ) -> tuple[str, ...]:
        statement = select(paper.c.id).where(paper.c.id.not_in(self._triaged(topic_id, topic_hash)))
        if since:
            statement = statement.where(
                func.coalesce(paper.c.published_at, paper.c.first_seen_at) >= since
            )
        rows = self._db.fetch(statement.order_by(paper.c.id))
        return tuple(row.id for row in rows)

    def save_triage(self, result: TriageResult, now: str) -> None:
        self._db.write(
            insert(triage)
            .values(
                paper_id=result.paper_id,
                topic_id=result.topic_id,
                topic_hash=result.topic_hash,
                relevant=result.relevant,
                reason=result.reason,
                prompt_version=result.prompt_version,
                model=result.model,
                created_at=now,
            )
            .on_conflict_do_nothing()
        )

    def relevant_papers(
        self, topic_id: str, topic_hash: str, since: str | None = None
    ) -> tuple[str, ...]:
        statement = (
            select(triage.c.paper_id)
            .join(paper, paper.c.id == triage.c.paper_id)
            .where(
                triage.c.topic_id == topic_id,
                triage.c.topic_hash == topic_hash,
                triage.c.relevant.is_(True),
            )
        )
        if since:
            statement = statement.where(
                func.coalesce(paper.c.published_at, paper.c.first_seen_at) >= since
            )
        rows = self._db.fetch(statement.order_by(triage.c.paper_id))
        return tuple(row.paper_id for row in rows)

    def relevant_topics(self, paper_id: str) -> tuple[tuple[str, str], ...]:
        """初筛判为相关的 (主题, 主题指纹)。"""
        rows = self._db.fetch(
            select(triage.c.topic_id, triage.c.topic_hash)
            .where(triage.c.paper_id == paper_id, triage.c.relevant.is_(True))
            .order_by(triage.c.topic_id)
        )
        return tuple((row.topic_id, row.topic_hash) for row in rows)

    def relevant_without_fulltext(self, topic_id: str, topic_hash: str) -> tuple[str, ...]:
        rows = self._db.fetch(
            select(triage.c.paper_id)
            .where(
                triage.c.topic_id == topic_id,
                triage.c.topic_hash == topic_hash,
                triage.c.relevant.is_(True),
                triage.c.paper_id.not_in(select(fulltext.c.paper_id)),
            )
            .order_by(triage.c.paper_id)
        )
        return tuple(row.paper_id for row in rows)

    def assessment_keys(self, paper_id: str, topic_id: str) -> tuple[AssessmentKey, ...]:
        rows = self._db.fetch(
            select(
                assessment.c.semantic_hash,
                assessment.c.topic_hash,
                assessment.c.input_hash,
                assessment.c.fulltext_used,
            ).where(assessment.c.paper_id == paper_id, assessment.c.topic_id == topic_id)
        )
        return tuple(
            AssessmentKey(
                row.semantic_hash, row.topic_hash, row.input_hash, bool(row.fulltext_used)
            )
            for row in rows
        )

    def insert_assessment(self, item: NewAssessment, now: str) -> None:
        self._db.write(
            insert(assessment)
            .values(
                paper_id=item.paper_id,
                topic_id=item.topic_id,
                topic_hash=item.topic_hash,
                semantic_hash=item.semantic_hash,
                input_hash=item.input_hash,
                levels=json.dumps(item.levels, sort_keys=True),
                evidence=json.dumps(item.evidence, ensure_ascii=False, sort_keys=True),
                fulltext_used=item.fulltext_used,
                model=item.model,
                prompt_version=item.prompt_version,
                created_at=now,
                goal_links=json.dumps(
                    [link.model_dump(mode="json") for link in item.goal_links],
                    ensure_ascii=False,
                    sort_keys=True,
                ),
            )
            .on_conflict_do_nothing()
        )

    def get_assessment(self, assessment_id: int) -> Assessment | None:
        rows = self._db.fetch(select(assessment).where(assessment.c.id == assessment_id))
        return _to_assessment(rows[0]) if rows else None

    def unscored_assessments(
        self, topic_id: str, semantic_hash: str, policy_hash: str
    ) -> tuple[int, ...]:
        scored = select(score.c.assessment_id).where(score.c.policy_hash == policy_hash)
        rows = self._db.fetch(
            select(assessment.c.id)
            .where(
                assessment.c.topic_id == topic_id,
                assessment.c.semantic_hash == semantic_hash,
                assessment.c.id.not_in(scored),
            )
            .order_by(assessment.c.id)
        )
        return tuple(row.id for row in rows)

    def save_score(
        self, assessment_id: int, policy_hash: str, result: ScoreResult, now: str
    ) -> None:
        self._db.write(
            insert(score)
            .values(
                assessment_id=assessment_id,
                policy_hash=policy_hash,
                score=result.score,
                tier=result.tier,
                provisional=result.provisional,
                undetermined=json.dumps(list(result.undetermined)),
                created_at=now,
            )
            .on_conflict_do_nothing()
        )

    def save_rubric_snapshot(self, hash_: str, rubric_id: str, body: str, now: str) -> None:
        self._db.write(
            insert(rubric_snapshot)
            .values(hash=hash_, rubric_id=rubric_id, body=body, created_at=now)
            .on_conflict_do_nothing()
        )

    def rank_candidates(self, query: RankQuery) -> tuple[ScoredAssessment, ...]:
        """只有当前主题定义和当前指标下的最新评估才参与排序。

        同一篇论文在同一渠道只推一次，不分主题；以临时分推过的，正式评估后可以再推一次。
        """
        latest = (
            select(func.max(assessment.c.id))
            .where(
                assessment.c.topic_id == query.topic_id,
                assessment.c.topic_hash == query.topic_hash,
                assessment.c.semantic_hash == query.semantic_hash,
            )
            .group_by(assessment.c.paper_id)
        )
        delivered = (
            select(digest_item.c.paper_id)
            .join(digest, digest.c.id == digest_item.c.digest_id)
            .where(digest.c.channel == query.channel)
        )
        delivered_formal = delivered.where(digest_item.c.provisional.is_(False))
        rows = self._db.fetch(
            select(*_SCORED_COLUMNS)
            .join(score, score.c.assessment_id == assessment.c.id)
            .where(
                assessment.c.id.in_(latest),
                score.c.policy_hash == query.policy_hash,
                score.c.tier.in_(query.tiers),
                assessment.c.paper_id.not_in(delivered_formal),
                or_(assessment.c.paper_id.not_in(delivered), score.c.provisional.is_(False)),
            )
            .order_by(score.c.score.desc(), assessment.c.paper_id)
            .limit(query.limit)
        )
        return tuple(_to_scored(row) for row in rows)

    def scored_for_paper(self, paper_id: str) -> tuple[ScoredAssessment, ...]:
        rows = self._db.fetch(
            select(*_SCORED_COLUMNS)
            .join(score, score.c.assessment_id == assessment.c.id)
            .where(assessment.c.paper_id == paper_id)
            .order_by(assessment.c.id, score.c.created_at, score.c.policy_hash)
        )
        return tuple(_to_scored(row) for row in rows)

    def papers_with_scores(self) -> tuple[str, ...]:
        rows = self._db.fetch(
            select(assessment.c.paper_id)
            .join(score, score.c.assessment_id == assessment.c.id)
            .distinct()
            .order_by(assessment.c.paper_id)
        )
        return tuple(row.paper_id for row in rows)
