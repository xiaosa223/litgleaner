"""升级兼容断言只接受搜索副本包装，不容许旧触发器业务SQL被改写。"""

import sqlite3

import pytest
from alembic import command
from tests.integration.relation_migration_helpers import assert_search_upgrade_triggers

from litgleaner.infra.db.migrate import alembic_config, upgrade_database


def _triggers(path):
    with sqlite3.connect(path) as connection:
        return dict(connection.execute("SELECT name,sql FROM sqlite_master WHERE type='trigger'"))


@pytest.fixture
def upgraded_triggers(tmp_path):
    path = tmp_path / "trigger-contract.db"
    url = f"sqlite:///{path}"
    command.upgrade(alembic_config(url), "0019")
    before = _triggers(path)
    upgrade_database(url)
    return before, _triggers(path)


def test_trigger_helper_accepts_only_real_normalized_projection_wrapping(upgraded_triggers):
    before, current = upgraded_triggers
    assert_search_upgrade_triggers(before, current)


@pytest.mark.parametrize(
    "original,replacement",
    [
        ("WHERE p.id=new.id", "WHERE p.id=old.id"),
        ("paper_id=new.id", "paper_id=old.id"),
        ("p.title,", "upper(p.title),"),
    ],
)
def test_trigger_helper_rejects_changed_where_delete_and_raw_projection(
    upgraded_triggers, original, replacement
):
    before, current = upgraded_triggers
    name = "search_paper_update"
    changed = current[name].replace(original, replacement, 1)
    assert changed != current[name]
    with pytest.raises(AssertionError):
        assert_search_upgrade_triggers(before, current | {name: changed})
