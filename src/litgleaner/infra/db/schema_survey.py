"""有界调研的计划、恢复用工作集和只追加轮次。"""

from sqlalchemy import (
    CheckConstraint,
    Column,
    Float,
    ForeignKey,
    ForeignKeyConstraint,
    Index,
    Integer,
    MetaData,
    String,
    Table,
    Text,
    UniqueConstraint,
)


def _json_object() -> CheckConstraint:
    return CheckConstraint("CASE WHEN json_valid(data) THEN json_type(data)='object' ELSE 0 END")


def _nonempty(*names: str) -> list[CheckConstraint]:
    return [CheckConstraint(f"length(trim({name}))>0") for name in names]


def _integer(*names: str) -> list[CheckConstraint]:
    return [CheckConstraint(f"typeof({name})='integer' AND {name}>=0") for name in names]


def _preview(metadata: MetaData) -> Table:
    return Table(
        "survey_preview",
        metadata,
        Column("id", String, primary_key=True),
        Column("topic_id", String, nullable=False),
        Column("topic_hash", String, nullable=False),
        Column("fingerprint", String, nullable=False),
        Column("created_at", String, nullable=False),
        Column("data", Text, nullable=False),
        Column("accepted_run", String),
        Column("accepted_at", String),
        *_nonempty("id", "topic_id", "topic_hash", "fingerprint", "created_at"),
        _json_object(),
        CheckConstraint(
            "(accepted_run IS NULL AND accepted_at IS NULL) OR "
            "(accepted_run IS NOT NULL AND length(trim(accepted_run))>0 AND "
            "accepted_at IS NOT NULL AND length(trim(accepted_at))>0)"
        ),
        Index("ix_survey_preview_topic", "topic_id", "created_at"),
    )


def _work(metadata: MetaData) -> Table:
    return Table(
        "survey_work",
        metadata,
        Column("run_id", String, primary_key=True),
        Column("iteration", Integer, primary_key=True),
        Column("plan_id", String, ForeignKey("survey_preview.id"), nullable=False),
        Column("topic_id", String, nullable=False),
        Column("topic_hash", String, nullable=False),
        Column("scope_id", String, nullable=False),
        Column("started_at", String, nullable=False),
        Column("data", Text, nullable=False),
        *_nonempty("run_id", "plan_id", "topic_id", "topic_hash", "scope_id", "started_at"),
        CheckConstraint("typeof(iteration)='integer' AND iteration>=1"),
        _json_object(),
    )


def _round(metadata: MetaData) -> Table:
    return Table(
        "survey_round",
        metadata,
        Column("topic_id", String, primary_key=True),
        Column("number", Integer, primary_key=True),
        Column("topic_hash", String, nullable=False),
        Column("run_id", String, nullable=False),
        Column("iteration", Integer, nullable=False),
        Column("plan_id", String, ForeignKey("survey_preview.id"), nullable=False),
        Column("started_at", String, nullable=False),
        Column("finished_at", String, nullable=False),
        Column("candidates", Integer, nullable=False),
        Column("new_papers", Integer, nullable=False),
        Column("passed", Integer, nullable=False),
        Column("new_passed", Integer, nullable=False),
        Column("undetermined", Integer, nullable=False),
        Column("cost_usd", Float, nullable=False),
        Column("data", Text, nullable=False),
        *_nonempty("topic_id", "topic_hash", "run_id", "plan_id", "started_at", "finished_at"),
        *_integer(
            "number",
            "iteration",
            "candidates",
            "new_papers",
            "passed",
            "new_passed",
            "undetermined",
        ),
        CheckConstraint("number>=1 AND iteration>=1"),
        CheckConstraint("new_passed<=passed AND passed<=candidates"),
        CheckConstraint("new_passed<=new_papers AND new_papers<=candidates"),
        CheckConstraint("undetermined<=candidates AND cost_usd>=0"),
        _json_object(),
        UniqueConstraint("run_id", "iteration"),
        ForeignKeyConstraint(
            ["run_id", "iteration"], ["survey_work.run_id", "survey_work.iteration"]
        ),
    )


def define_survey(metadata: MetaData) -> tuple[Table, Table, Table]:
    return _preview(metadata), _work(metadata), _round(metadata)
