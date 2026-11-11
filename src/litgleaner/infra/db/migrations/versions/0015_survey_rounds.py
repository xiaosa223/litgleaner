"""Immutable survey plans, resumable work, append-only rounds, scoped LLM billing.

Revision ID: 0015
Revises: 0014
"""

from alembic import op

revision = "0015"
down_revision = "0014"
branch_labels = None
depends_on = None


def _execute(sql: str) -> None:
    op.get_bind().exec_driver_sql(sql)


def _object() -> str:
    return "CHECK(CASE WHEN json_valid(data) THEN json_type(data)='object' ELSE 0 END)"


def _nonempty(*names: str) -> str:
    return ",".join(f"CHECK(length(trim({name}))>0)" for name in names)


def _create_preview() -> None:
    _execute(
        "CREATE TABLE survey_preview (id VARCHAR PRIMARY KEY NOT NULL,"
        "topic_id VARCHAR NOT NULL,topic_hash VARCHAR NOT NULL,fingerprint VARCHAR NOT NULL,"
        "created_at VARCHAR NOT NULL,data TEXT NOT NULL,accepted_run VARCHAR,accepted_at VARCHAR,"
        + _object()
        + ","
        + _nonempty("id", "topic_id", "topic_hash", "fingerprint", "created_at")
        + ","
        "CHECK((accepted_run IS NULL AND accepted_at IS NULL) OR "
        "(accepted_run IS NOT NULL AND length(trim(accepted_run))>0 AND "
        "accepted_at IS NOT NULL AND length(trim(accepted_at))>0)))"
    )
    _execute("CREATE INDEX ix_survey_preview_topic ON survey_preview(topic_id,created_at)")


def _create_work() -> None:
    _execute(
        "CREATE TABLE survey_work (run_id VARCHAR NOT NULL,iteration INTEGER NOT NULL,"
        "plan_id VARCHAR NOT NULL REFERENCES survey_preview(id),topic_id VARCHAR NOT NULL,"
        "topic_hash VARCHAR NOT NULL,scope_id VARCHAR NOT NULL,started_at VARCHAR NOT NULL,"
        "data TEXT NOT NULL,PRIMARY KEY(run_id,iteration),"
        "CHECK(typeof(iteration)='integer' AND iteration>=1),"
        + _object()
        + ","
        + _nonempty("run_id", "plan_id", "topic_id", "topic_hash", "scope_id", "started_at")
        + ")"
    )


def _create_round() -> None:
    integers = (
        "number",
        "iteration",
        "candidates",
        "new_papers",
        "passed",
        "new_passed",
        "undetermined",
    )
    _execute(
        "CREATE TABLE survey_round (topic_id VARCHAR NOT NULL,number INTEGER NOT NULL,"
        "topic_hash VARCHAR NOT NULL,run_id VARCHAR NOT NULL,iteration INTEGER NOT NULL,"
        "plan_id VARCHAR NOT NULL REFERENCES survey_preview(id),started_at VARCHAR NOT NULL,"
        "finished_at VARCHAR NOT NULL,candidates INTEGER NOT NULL,new_papers INTEGER NOT NULL,"
        "passed INTEGER NOT NULL,new_passed INTEGER NOT NULL,undetermined INTEGER NOT NULL,"
        "cost_usd FLOAT NOT NULL,data TEXT NOT NULL,PRIMARY KEY(topic_id,number),"
        "UNIQUE(run_id,iteration),"
        "FOREIGN KEY(run_id,iteration) REFERENCES survey_work(run_id,iteration),"
        + ",".join(f"CHECK(typeof({name})='integer' AND {name}>=0)" for name in integers)
        + ","
        "CHECK(number>=1 AND iteration>=1),CHECK(new_passed<=passed AND passed<=candidates),"
        "CHECK(new_passed<=new_papers AND new_papers<=candidates),"
        "CHECK(undetermined<=candidates AND cost_usd>=0),"
        + _object()
        + ","
        + _nonempty("topic_id", "topic_hash", "run_id", "plan_id", "started_at", "finished_at")
        + ")"
    )


def _identity_trigger(table: str, columns: tuple[str, ...]) -> None:
    invalid = " OR ".join(f"json_extract(new.data,'$.{key}') IS NOT new.{key}" for key in columns)
    _execute(
        f"CREATE TRIGGER {table}_json BEFORE INSERT ON {table} WHEN "
        f"CASE WHEN json_valid(new.data) THEN ({invalid}) ELSE 0 END "
        f"BEGIN SELECT RAISE(ABORT, '{table}_json_invalid'); END"
    )


def _triggers() -> None:
    _execute(
        "CREATE TRIGGER survey_preview_immutable BEFORE UPDATE ON survey_preview WHEN "
        "new.id IS NOT old.id OR new.topic_id IS NOT old.topic_id OR "
        "new.topic_hash IS NOT old.topic_hash OR new.fingerprint IS NOT old.fingerprint OR "
        "new.created_at IS NOT old.created_at OR new.data IS NOT old.data OR "
        "old.accepted_run IS NOT NULL OR new.accepted_run IS NULL "
        "BEGIN SELECT RAISE(ABORT, 'survey_preview_immutable'); END"
    )
    for table in ("survey_work", "survey_round"):
        _execute(
            f"CREATE TRIGGER {table}_immutable BEFORE UPDATE ON {table} "
            f"BEGIN SELECT RAISE(ABORT, '{table}_immutable'); END"
        )
    _execute(
        "CREATE TRIGGER survey_round_no_delete BEFORE DELETE ON survey_round "
        "BEGIN SELECT RAISE(ABORT, 'survey_round_immutable'); END"
    )
    _identity_trigger(
        "survey_preview", ("id", "topic_id", "topic_hash", "fingerprint", "created_at")
    )
    _identity_trigger(
        "survey_work",
        ("run_id", "iteration", "plan_id", "topic_id", "topic_hash", "scope_id", "started_at"),
    )
    _identity_trigger(
        "survey_round",
        (
            "topic_id",
            "number",
            "topic_hash",
            "run_id",
            "iteration",
            "plan_id",
            "started_at",
            "finished_at",
            "candidates",
            "new_papers",
            "passed",
            "new_passed",
            "undetermined",
            "cost_usd",
        ),
    )


def upgrade() -> None:
    _create_preview()
    _create_work()
    _create_round()
    _triggers()
    _execute("ALTER TABLE llm_call ADD COLUMN scope_id VARCHAR")
    op.create_index("ix_llm_call_scope_id", "llm_call", ["scope_id"])


def downgrade() -> None:
    op.drop_index("ix_llm_call_scope_id", table_name="llm_call")
    _execute("ALTER TABLE llm_call DROP COLUMN scope_id")
    _execute("DROP TRIGGER survey_round_no_delete")
    for table in ("survey_round", "survey_work", "survey_preview"):
        _execute(f"DROP TRIGGER {table}_immutable")
        _execute(f"DROP TRIGGER {table}_json")
        op.drop_table(table)
