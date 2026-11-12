"""Durable first admission and frozen verified paper relations.

Revision ID: 0019
Revises: 0018
"""

from alembic import op

revision = "0019"
down_revision = "0018"
branch_labels = None
depends_on = None

_BATCH_IDENTITY = ("id", "paper_id", "input_hash", "producer_version", "created_at")
_RELATION_IDENTITY = ("id", "batch_id", "paper_a", "paper_b", "kind", "source", "created_at")


def _execute(sql: str) -> None:
    op.get_bind().exec_driver_sql(sql)


def _admission() -> None:
    _execute(
        "CREATE TABLE memory_admission (id INTEGER PRIMARY KEY NOT NULL,"
        "paper_id VARCHAR NOT NULL UNIQUE REFERENCES paper(id),configuration TEXT NOT NULL,"
        "created_at VARCHAR NOT NULL,baseline BOOLEAN NOT NULL, CHECK(id>0),"
        "CHECK(length(trim(paper_id))>0),CHECK(length(trim(created_at))>0),"
        "CHECK(baseline IN (0,1)),CHECK(CASE WHEN json_valid(configuration) "
        "THEN json_type(configuration)='object' ELSE 0 END))"
    )
    _execute(
        "INSERT INTO memory_admission(paper_id,configuration,created_at,baseline) "
        "SELECT id,'{}',strftime('%Y-%m-%dT%H:%M:%fZ','now'),1 FROM paper WHERE "
        "EXISTS(SELECT 1 FROM reading_state WHERE paper_id=paper.id) OR "
        "EXISTS(SELECT 1 FROM annotation WHERE paper_id=paper.id AND deleted_at IS NULL) OR "
        "EXISTS(SELECT 1 FROM inbox WHERE paper_id=paper.id AND status='done') ORDER BY paper.id"
    )
    _immutable("memory_admission", "paper_id=new.paper_id OR id=new.id")


def _batch() -> None:
    _execute(
        "CREATE TABLE relate_batch (id VARCHAR PRIMARY KEY NOT NULL,"
        "paper_id VARCHAR NOT NULL REFERENCES paper(id),input_hash VARCHAR NOT NULL,"
        "producer_version VARCHAR NOT NULL,created_at VARCHAR NOT NULL,data TEXT NOT NULL,"
        "UNIQUE(paper_id,input_hash,producer_version),"
        "CHECK(CASE WHEN json_valid(data) THEN json_type(data)='object' ELSE 0 END),"
        + ",".join(f"CHECK(length(trim({name}))>0)" for name in _BATCH_IDENTITY)
        + ")"
    )
    _execute("CREATE INDEX ix_relate_batch_paper_created ON relate_batch(paper_id,created_at,id)")
    _immutable(
        "relate_batch",
        "id=new.id OR (paper_id=new.paper_id AND input_hash=new.input_hash "
        "AND producer_version=new.producer_version)",
    )
    _json_identity("relate_batch", _BATCH_IDENTITY)


def _relation() -> None:
    _execute(
        "CREATE TABLE paper_relation (id VARCHAR PRIMARY KEY NOT NULL,"
        "batch_id VARCHAR NOT NULL REFERENCES relate_batch(id),"
        "paper_a VARCHAR NOT NULL REFERENCES paper(id),"
        "paper_b VARCHAR NOT NULL REFERENCES paper(id),"
        "pair_a VARCHAR NOT NULL,pair_b VARCHAR NOT NULL,"
        "kind VARCHAR NOT NULL,source VARCHAR NOT NULL,"
        "status VARCHAR NOT NULL,feedback_id VARCHAR UNIQUE REFERENCES feedback(id),"
        "superseded_by VARCHAR REFERENCES paper_relation(id),"
        "created_at VARCHAR NOT NULL,data TEXT NOT NULL,"
        "CHECK(paper_a<>paper_b AND pair_a=min(paper_a,paper_b) AND pair_b=max(paper_a,paper_b)),"
        "CHECK(kind IN ('extends','uses_method_of','compares_with',"
        "'contradicts','similar_approach','same_problem')),"
        "CHECK(kind NOT IN ('contradicts','similar_approach','same_problem') OR paper_a<paper_b),"
        "CHECK(source IN ('model','user')),CHECK(source<>'user' OR feedback_id IS NOT NULL),"
        "CHECK(status IN ('valid','superseded','rejected_by_user')),"
        "CHECK((status='superseded')=(superseded_by IS NOT NULL) AND superseded_by IS NOT id),"
        "CHECK(CASE WHEN json_valid(data) THEN json_type(data)='object' ELSE 0 END),"
        + ",".join(
            f"CHECK(length(trim({name}))>0)"
            for name in ("id", "batch_id", "paper_a", "paper_b", "created_at")
        )
        + ")"
    )
    _execute(
        "CREATE INDEX ix_paper_relation_pair_created ON paper_relation(pair_a,pair_b,created_at,id)"
    )
    _execute("CREATE INDEX ix_paper_relation_b_created ON paper_relation(paper_b,created_at,id)")
    _execute("CREATE INDEX ix_paper_relation_a_created ON paper_relation(paper_a,created_at,id)")
    _execute(
        "CREATE UNIQUE INDEX uq_relation_model_batch_pair "
        "ON paper_relation(batch_id,pair_a,pair_b) WHERE source='model'"
    )
    _immutable(
        "paper_relation",
        "id=new.id OR (source='model' AND new.source='model' AND batch_id=new.batch_id "
        "AND pair_a=new.pair_a AND pair_b=new.pair_b) "
        "OR (feedback_id IS NOT NULL AND feedback_id=new.feedback_id)",
        allow_state=True,
    )
    _json_identity("paper_relation", _RELATION_IDENTITY)
    _relation_guards()


def _immutable(table: str, conflict: str, *, allow_state: bool = False) -> None:
    for event in ("DELETE", "UPDATE"):
        condition = ""
        if allow_state and event == "UPDATE":
            columns = (*_RELATION_IDENTITY, "pair_a", "pair_b", "feedback_id", "data", "rowid")
            condition = " WHEN " + " OR ".join(f"new.{name} IS NOT old.{name}" for name in columns)
        _execute(
            f"CREATE TRIGGER {table}_no_{event.lower()} BEFORE {event} ON {table}{condition} "
            f"BEGIN SELECT RAISE(ABORT, '{table}_immutable'); END"
        )
    _execute(
        f"CREATE TRIGGER {table}_no_replace BEFORE INSERT ON {table} WHEN EXISTS("
        f"SELECT 1 FROM {table} WHERE {conflict} OR rowid=new.rowid) "
        f"BEGIN SELECT RAISE(ABORT, '{table}_immutable'); END"
    )
    _execute(
        f"CREATE TRIGGER {table}_rowid AFTER INSERT ON {table} WHEN new.rowid<=0 "
        f"BEGIN SELECT RAISE(ABORT, '{table}_rowid_invalid'); END"
    )


def _json_identity(table: str, fields: tuple[str, ...]) -> None:
    invalid = " OR ".join(
        f"json_type(new.data,'$.{name}') IS NOT 'text' "
        f"OR json_extract(new.data,'$.{name}') IS NOT new.{name}"
        for name in fields
    )
    _execute(
        f"CREATE TRIGGER {table}_json BEFORE INSERT ON {table} "
        f"WHEN CASE WHEN json_valid(new.data) THEN ({invalid}) ELSE 0 END "
        f"BEGIN SELECT RAISE(ABORT, '{table}_json_invalid'); END"
    )


def _relation_guards() -> None:
    _execute(
        "CREATE TRIGGER paper_relation_source BEFORE INSERT ON paper_relation WHEN "
        "CASE WHEN json_valid(new.data) THEN (NOT EXISTS("
        "SELECT 1 FROM relate_batch WHERE id=new.batch_id "
        "AND paper_id IN (new.paper_a,new.paper_b) "
        "AND input_hash=json_extract(new.data,'$.input_hash') "
        "AND producer_version=json_extract(new.data,'$.producer_version')) "
        "OR json_extract(new.data,'$.evidence_a.paper_id') IS NOT new.paper_a "
        "OR json_extract(new.data,'$.evidence_b.paper_id') IS NOT new.paper_b "
        "OR json_extract(new.data,'$.feedback_id') IS NOT new.feedback_id) ELSE 0 END "
        "BEGIN SELECT RAISE(ABORT, 'paper_relation_source_invalid'); END"
    )
    for event in ("INSERT", "UPDATE"):
        _execute(
            f"CREATE TRIGGER paper_relation_supersession_{event.lower()} BEFORE {event} "
            "ON paper_relation WHEN new.superseded_by IS NOT NULL AND NOT EXISTS("
            "SELECT 1 FROM paper_relation WHERE id=new.superseded_by "
            "AND pair_a=new.pair_a AND pair_b=new.pair_b) "
            "BEGIN SELECT RAISE(ABORT, 'paper_relation_supersession_invalid'); END"
        )
    _execute(
        "CREATE TRIGGER paper_relation_library BEFORE INSERT ON paper_relation WHEN EXISTS("
        "SELECT 1 FROM paper WHERE id IN (new.paper_a,new.paper_b) AND NOT("
        "EXISTS(SELECT 1 FROM reading_state WHERE paper_id=paper.id) OR "
        "EXISTS(SELECT 1 FROM annotation WHERE paper_id=paper.id AND deleted_at IS NULL) OR "
        "EXISTS(SELECT 1 FROM inbox WHERE paper_id=paper.id AND status='done'))) "
        "BEGIN SELECT RAISE(ABORT, 'paper_relation_library_required'); END"
    )
    _evidence_guards()


def _evidence_guards() -> None:
    invalid: list[str] = []
    for side in ("a", "b"):
        path = f"$.evidence_{side}"
        invalid.extend(
            (
                f"json_type(new.data,'{path}.page') IS NOT 'integer'",
                f"json_extract(new.data,'{path}.page')<0",
                f"json_type(new.data,'{path}.doc_key') IS NOT 'text'",
                f"length(trim(json_extract(new.data,'{path}.doc_key')))=0",
                f"json_type(new.data,'{path}.quote') IS NOT 'text'",
                f"length(json_extract(new.data,'{path}.quote')) NOT BETWEEN 10 AND 3000",
            )
        )
    _execute(
        "CREATE TRIGGER paper_relation_evidence BEFORE INSERT ON paper_relation WHEN "
        f"CASE WHEN json_valid(new.data) THEN ({' OR '.join(invalid)}) ELSE 0 END "
        "BEGIN SELECT RAISE(ABORT, 'paper_relation_evidence_invalid'); END"
    )


def upgrade() -> None:
    _admission()
    _batch()
    _relation()
    _execute("ALTER TABLE feedback ADD COLUMN relation_id VARCHAR REFERENCES paper_relation(id)")
    _execute("CREATE INDEX ix_feedback_relation_id ON feedback(relation_id)")
    _execute(
        "ALTER TABLE feedback ADD COLUMN payload TEXT CHECK(payload IS NULL "
        "OR CASE WHEN json_valid(payload) THEN json_type(payload)='object' ELSE 0 END)"
    )


def downgrade() -> None:
    _execute("DROP INDEX ix_feedback_relation_id")
    _execute("ALTER TABLE feedback DROP COLUMN payload")
    _execute("ALTER TABLE feedback DROP COLUMN relation_id")
    for table in ("paper_relation", "relate_batch", "memory_admission"):
        op.drop_table(table)
