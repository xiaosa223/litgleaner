"""个性化引导与掌握程度（第 12.7 节）：导读缓存、回答卡片、评价和掌握事件，全部只追加。"""

from sqlalchemy import (
    CheckConstraint,
    Column,
    ForeignKey,
    Index,
    Integer,
    MetaData,
    String,
    Table,
    Text,
    UniqueConstraint,
)

_JSON = "CASE WHEN json_valid(data) THEN json_type(data)='object' ELSE 0 END"


def _explanation(metadata: MetaData) -> Table:
    return Table(
        "explanation",
        metadata,
        Column("id", String, primary_key=True),
        Column("paper_id", String, ForeignKey("paper.id"), nullable=False),
        Column("kind", String, nullable=False),
        Column("fingerprint", String, nullable=False),
        Column("producer_version", String, nullable=False),
        Column("reading_language", String, nullable=False),
        Column("created_at", String, nullable=False),
        Column("data", Text, nullable=False),
        CheckConstraint("kind IN ('guide')", name="ck_explanation_kind"),
        CheckConstraint(_JSON, name="ck_explanation_data"),
        UniqueConstraint("paper_id", "kind", "fingerprint", "producer_version"),
        Index("ix_explanation_paper", "paper_id", "kind", "created_at", "id"),
    )


def _answer_card(metadata: MetaData) -> Table:
    return Table(
        "answer_card",
        metadata,
        Column("id", String, primary_key=True),
        Column("paper_id", String, ForeignKey("paper.id"), nullable=False),
        Column("explanation_id", String, ForeignKey("explanation.id"), nullable=False),
        Column("question_index", Integer, nullable=False),
        Column("revision", Integer, nullable=False),
        Column("text", Text, nullable=False),
        Column("created_at", String, nullable=False),
        CheckConstraint("question_index BETWEEN 0 AND 2", name="ck_answer_card_question"),
        CheckConstraint("revision >= 1", name="ck_answer_card_revision"),
        CheckConstraint(
            "length(trim(text)) > 0 AND length(text) <= 2000", name="ck_answer_card_text"
        ),
        UniqueConstraint("explanation_id", "question_index", "revision"),
        Index("ix_answer_card_paper", "paper_id", "created_at"),
    )


def _answer_evaluation(metadata: MetaData) -> Table:
    return Table(
        "answer_evaluation",
        metadata,
        Column("id", String, primary_key=True),
        Column("card_id", String, ForeignKey("answer_card.id"), nullable=False, unique=True),
        Column("model", String, nullable=False),
        Column("prompt_version", String, nullable=False),
        Column("input_hash", String, nullable=False),
        Column("created_at", String, nullable=False),
        Column("data", Text, nullable=False),
        CheckConstraint(_JSON, name="ck_answer_evaluation_data"),
    )


def _mastery_event(metadata: MetaData) -> Table:
    return Table(
        "mastery_event",
        metadata,
        Column("id", String, primary_key=True),
        Column("concept_id", String, ForeignKey("concept.id"), nullable=False),
        Column("kind", String, nullable=False),
        Column("level", Integer),
        Column("rating", String),
        Column("question_kind", String),
        Column("verdict", String),
        Column("overall", String),
        Column("source_id", String),
        Column("created_at", String, nullable=False),
        CheckConstraint(
            "(kind='override' AND level BETWEEN 0 AND 3 AND rating IS NULL AND verdict IS NULL "
            "AND question_kind IS NULL AND overall IS NULL) "
            "OR (kind='evaluation' AND level IS NULL AND rating IS NULL "
            "AND question_kind IN ('understand','compare','transfer') "
            "AND verdict IN ('correct','missing','misunderstood') "
            "AND overall IN ('correct','partial','incorrect')) "
            "OR (kind='review' AND level IS NULL AND verdict IS NULL "
            "AND rating IN ('remembered','fuzzy','forgot'))",
            name="ck_mastery_event_kind",
        ),
        Index("ix_mastery_event_concept", "concept_id", "created_at", "id"),
    )


def define_correction(metadata: MetaData) -> Table:
    """用户修正的论文元数据（ADR 0011）：只追加，同一字段以最新一条为准。"""
    return Table(
        "paper_correction",
        metadata,
        Column("id", String, primary_key=True),
        Column("paper_id", String, ForeignKey("paper.id"), nullable=False),
        Column("field", String, nullable=False),
        Column("value", Text, nullable=False),
        Column("created_at", String, nullable=False),
        CheckConstraint(
            "field IN ('title','authors','venue','published_at')", name="ck_paper_correction_field"
        ),
        CheckConstraint("length(value) <= 2000", name="ck_paper_correction_value"),
        Index("ix_paper_correction_paper", "paper_id", "field", "created_at", "id"),
    )


def define_concept_links(metadata: MetaData) -> tuple[Table, Table, Table]:
    """概念关系、疑似重复与用户决定（第 12.7 节），全部只追加。"""
    link = Table(
        "concept_link",
        metadata,
        Column("id", String, primary_key=True),
        Column("concept_a", String, ForeignKey("concept.id"), nullable=False),
        Column("concept_b", String, ForeignKey("concept.id"), nullable=False),
        Column("kind", String, nullable=False),
        Column("reason", Text, nullable=False),
        Column("guide_id", String, ForeignKey("explanation.id"), nullable=False),
        Column("created_at", String, nullable=False),
        CheckConstraint(
            "kind IN ('prerequisite','similar','contrast')", name="ck_concept_link_kind"
        ),
        CheckConstraint("concept_a <> concept_b", name="ck_concept_link_pair"),
        UniqueConstraint("concept_a", "concept_b", "kind"),
    )
    merge = Table(
        "concept_merge_candidate",
        metadata,
        Column("id", String, primary_key=True),
        Column("concept_a", String, ForeignKey("concept.id"), nullable=False),
        Column("concept_b", String, ForeignKey("concept.id"), nullable=False),
        Column("reason", Text, nullable=False),
        Column("guide_id", String, ForeignKey("explanation.id"), nullable=False),
        Column("created_at", String, nullable=False),
        CheckConstraint("concept_a <> concept_b", name="ck_concept_merge_pair"),
        UniqueConstraint("concept_a", "concept_b"),
    )
    decision = Table(
        "concept_decision",
        metadata,
        Column("id", String, primary_key=True),
        Column("target", String, nullable=False),
        Column("decision", String, nullable=False),
        Column("created_at", String, nullable=False),
        CheckConstraint("decision IN ('confirmed','rejected')", name="ck_concept_decision"),
        Index("ix_concept_decision_target", "target", "created_at", "id"),
    )
    return link, merge, decision


LEARNING_TABLES = ("explanation", "answer_card", "answer_evaluation", "mastery_event")


def define_learning(metadata: MetaData) -> tuple[Table, Table, Table, Table]:
    return (
        _explanation(metadata),
        _answer_card(metadata),
        _answer_evaluation(metadata),
        _mastery_event(metadata),
    )
