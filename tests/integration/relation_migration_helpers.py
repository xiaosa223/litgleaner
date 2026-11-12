"""旧迁移的head验收只允许0019明确扩展反馈结构。"""

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


def assert_legacy_preserved(before, after, keys, current_keys, *, definitions, current_definitions):
    assert before.keys() == after.keys() == keys.keys() == current_keys.keys()
    for table in before:
        if table != "feedback":
            assert before[table] == after[table]
            assert keys[table] == current_keys[table]
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
