"""旧 zh 译文缓存无费用升级为 zh-CN，冲突时两份原记录都保留。"""

import sqlite3
from pathlib import Path

from alembic import command

from litgleaner.infra.db.migrate import alembic_config, upgrade_database


def test_legacy_cache_language_is_normalized_and_rows_survive_roundtrip(tmp_path: Path):
    path = tmp_path / "translation.db"
    url = f"sqlite:///{path}"
    command.upgrade(alembic_config(url), "0008")
    with sqlite3.connect(path) as conn:
        for language, source in (
            ("zh", "old"),
            ("en", "english"),
            ("zh", "conflict"),
            ("zh-CN", "conflict"),
        ):
            conn.execute(
                "INSERT INTO translation"
                "(unit,source_hash,target_lang,glossary_hash,translator_version,"
                "sentences,model,created_at) VALUES ('block',?,?,'g','v','[\"cached\"]','m','t')",
                (source, language),
            )
    upgrade_database(url)
    with sqlite3.connect(path) as conn:
        assert conn.execute("SELECT count(*) FROM translation").fetchone()[0] == 4
        assert (
            conn.execute("SELECT target_lang FROM translation WHERE source_hash='old'").fetchone()[
                0
            ]
            == "zh-CN"
        )
        assert (
            conn.execute("SELECT sentences FROM translation WHERE source_hash='old'").fetchone()[0]
            == '["cached"]'
        )
        assert conn.execute("SELECT count(*) FROM llm_call").fetchone()[0] == 0
    command.downgrade(alembic_config(url), "0008")
    with sqlite3.connect(path) as conn:
        assert conn.execute("SELECT count(*) FROM translation").fetchone()[0] == 4
        assert (
            conn.execute("SELECT target_lang FROM translation WHERE source_hash='old'").fetchone()[
                0
            ]
            == "zh"
        )
