"""0019真实旧索引与规范记录，保留所有来源类型及文档标记。"""

from alembic import command
from sqlalchemy import text

from litgleaner.infra.db.database import Database
from litgleaner.infra.db.migrate import alembic_config

COLUMNS = "kind,id,paper_id,page,authorship,source_version,title,text"


def raw_rows(db):
    return tuple(
        tuple(row)
        for row in db.fetch(text(f"SELECT rowid,{COLUMNS} FROM memory_fts ORDER BY rowid"))
    )


def trigger_rows(db):
    return dict(
        db.fetch(
            text(
                "SELECT name,sql FROM sqlite_master WHERE type='trigger' AND "
                "(name LIKE 'search_%' OR name LIKE 'qa_search_%' "
                "OR name LIKE 'knowledge_search_%') "
                "ORDER BY name"
            )
        )
    )


def _paper_and_notes(db):
    db.write(
        text(
            "INSERT INTO paper(id,title,title_key,abstract,authors,venue,first_seen_at) "
            "VALUES ('P','Ｓｔｒａße Café','p','  原始  摘要 é  ',"
            "'[\"作者\"]','ＳＣＩＥＮＣＥ','t')"
        )
    )
    db.write(text("INSERT INTO paper_alias VALUES ('doi','10.1000/ＡＢＣ','P')"))
    db.write(
        text(
            "INSERT INTO annotation(id,paper_id,kind,page,quote,body,created_at,updated_at) "
            "VALUES ('N','P','note',3,'ＡＢＣ 作者原话','Straße　用户\n正文','t','t')"
        )
    )
    db.write(
        text(
            "INSERT INTO extraction(paper_id,input_hash,extractor_version,data,created_at) "
            "VALUES ('P','input','producer','{\"summary\":\"ＳＵＭＭＡＲＹ Straße\"}','t')"
        )
    )


def _memory(db):
    db.write(
        text(
            "INSERT INTO qa_message(id,paper_id,role,request_id,text,page,data,created_at) "
            "VALUES ('Q','P','question','request','用户问：Ｓｔｒａße？',3,'{}','t')"
        )
    )
    db.write(
        text(
            "INSERT INTO qa_message(id,paper_id,role,parent_id,text,page,data,created_at) "
            "VALUES ('A','P','answer','Q','模型答：Straße。',3,'{}','t')"
        )
    )
    db.write(text("INSERT INTO concept VALUES ('C','ＣＯＮＣＥＰＴ','t')"))
    db.write(text("INSERT INTO concept_alias VALUES ('strasse','C','Straße','de','t')"))
    db.write(
        text(
            "INSERT INTO concept_occurrence VALUES "
            "('C','P','core','作者定义保留 Café 和 Straße 字形',5,NULL,1,'input','producer','t')"
        )
    )


def legacy_database(path):
    url = f"sqlite:///{path}"
    command.upgrade(alembic_config(url), "0019")
    db = Database(url)
    with db.atomic():
        _paper_and_notes(db)
        _memory(db)
        for rowid, kind, id_, page, content in (
            (900, "fulltext", "0", 7, "  正文 Straße　é ＡＢＣ  "),
            (901, "document", "P", None, ""),
            (902, "fulltext", "null", 8, None),
        ):
            db.write(
                text(
                    f"INSERT INTO memory_fts(rowid,{COLUMNS}) "
                    "VALUES (:rowid,:kind,:id,'P',:page,'author','blob://original',NULL,:content)"
                ).bindparams(rowid=rowid, kind=kind, id=id_, page=page, content=content)
            )
    return url, db
