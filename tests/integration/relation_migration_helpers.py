"""旧迁移的head验收仅允许0019反馈扩展及0020确定性搜索副本。"""

import re

FTS_RAW_COLUMNS = "rowid,kind,id,paper_id,page,authorship,source_version,title,text"
_SEARCH_TRIGGERS = ("search_", "qa_search_", "knowledge_search_")
_NORMALIZE = "litgleaner_search_normalize_v1"

RELATION_TRIGGERS = {
    "memory_admission_no_update",
    "memory_admission_no_delete",
    "memory_admission_no_replace",
    "memory_admission_rowid",
    "relate_batch_no_update",
    "relate_batch_no_delete",
    "relate_batch_no_replace",
    "relate_batch_rowid",
    "relate_batch_json",
    "paper_relation_no_update",
    "paper_relation_no_delete",
    "paper_relation_no_replace",
    "paper_relation_rowid",
    "paper_relation_json",
    "paper_relation_source",
    "paper_relation_evidence",
    "paper_relation_supersession_insert",
    "paper_relation_supersession_update",
    "paper_relation_library",
}


def assert_feedback_extension(before, after, keys, current_keys):
    """校验旧字段原样保留，额外只含两个NULL列和指定FK。"""
    assert after == [(*row, None, None) for row in before]
    old = {tuple(row[2:]) for row in keys}
    expected = old | {("paper_relation", "relation_id", "id", "NO ACTION", "NO ACTION", "NONE")}
    assert {tuple(row[2:]) for row in current_keys} == expected
    assert len(current_keys) == len(keys) + 1


def assert_search_upgrade_triggers(before, current):
    """普通触发器SQL原样；搜索同名且显式保护UDF与两个规范副本。"""
    for name, sql in before.items():
        assert name in current
        upgraded = current[name]
        if not name.startswith(_SEARCH_TRIGGERS):
            assert upgraded == sql
            continue
        assert upgraded.count(f"BEGIN SELECT {_NORMALIZE}(NULL);") == 1
        inserts = sql.count("INSERT INTO memory_fts")
        assert upgraded.count("INSERT INTO memory_fts") == inserts
        if inserts:
            columns = FTS_RAW_COLUMNS.removeprefix("rowid,") + ",search_title,search_text"
            assert upgraded.count(f"INSERT INTO memory_fts({columns})") == inserts
            assert upgraded.count(f"{_NORMALIZE}(projected.title)") == inserts
            assert upgraded.count(f"{_NORMALIZE}(projected.text)") == inserts
        assert _restore_search_trigger(upgraded, inserts) == sql


def _restore_search_trigger(upgraded, inserts):
    """独立逆还原0020包装，确保原始DELETE、WHERE与八列投影逐字保留。"""
    columns = FTS_RAW_COLUMNS.removeprefix("rowid,")
    prefix = (
        f"INSERT INTO memory_fts({columns},search_title,search_text) "
        f"SELECT projected.*,{_NORMALIZE}(projected.title),"
        f"{_NORMALIZE}(projected.text) FROM (WITH projected({columns}) AS ("
    )
    suffix = ") SELECT * FROM projected) AS projected"
    wrapped = re.compile(re.escape(prefix) + "(.*?)" + re.escape(suffix), re.DOTALL)
    restored = upgraded.replace(f"BEGIN SELECT {_NORMALIZE}(NULL);", "BEGIN ", 1)
    restored, count = wrapped.subn(
        lambda match: f"INSERT INTO memory_fts({columns}) " + match[1], restored
    )
    assert count == inserts
    return restored


def _assert_search_index_definition(before, current):
    original, _rootpage = before
    upgraded, rootpage = current
    old_columns = "title,text,tokenize='trigram'"
    new_columns = "title UNINDEXED,text UNINDEXED,search_title,search_text,tokenize='trigram'"
    assert original.count(old_columns) == 1
    assert upgraded == original.replace(old_columns, new_columns, 1)
    assert type(rootpage) is int and rootpage >= 0


def assert_legacy_preserved(before, after, keys, current_keys, *, definitions, current_definitions):
    assert before.keys() == after.keys() == keys.keys() == current_keys.keys()
    for table in before:
        if table != "feedback":
            assert before[table] == after[table]
            assert keys[table] == current_keys[table]
            if table == "memory_fts":
                _assert_search_index_definition(definitions[table], current_definitions[table])
            else:
                assert definitions[table] == current_definitions[table]
            continue
        assert_feedback_extension(before[table], after[table], keys[table], current_keys[table])
        sql, rootpage = current_definitions[table]
        for fragment in (
            ", relation_id VARCHAR REFERENCES paper_relation(id)",
            ", payload TEXT CHECK(payload IS NULL OR CASE WHEN json_valid(payload) "
            "THEN json_type(payload)='object' ELSE 0 END)",
        ):
            assert sql.count(fragment) == 1
            sql = sql.replace(fragment, "", 1)
        assert (sql, rootpage) == definitions[table]
