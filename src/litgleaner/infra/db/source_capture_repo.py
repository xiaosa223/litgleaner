"""短事务保存采集凭据和连续输入；终结只允许一次。"""

import json
import re
from collections.abc import Mapping
from dataclasses import asdict
from datetime import UTC, datetime
from typing import Any, cast

from sqlalchemy import Row, func, insert, select, update

from litgleaner.infra.db.database import Database
from litgleaner.infra.db.schema import source_capture_attempt as attempt_table
from litgleaner.infra.db.schema import source_capture_input as input_table
from litgleaner.ports.source_capture import (
    CaptureAttempt,
    CapturedInput,
    CapturePurpose,
    CaptureScope,
    CaptureStatus,
    SourceRecipe,
)


def _text(value: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError("采集标识和文字字段不能为空")
    return value


def _stamp(value: str) -> int:
    parsed = datetime.fromisoformat(_text(value))
    delta = parsed.replace(tzinfo=UTC) if parsed.tzinfo is None else parsed.astimezone(UTC)
    elapsed = delta - datetime(1970, 1, 1, tzinfo=UTC)
    return (elapsed.days * 86400 + elapsed.seconds) * 1_000_000 + elapsed.microseconds


def _json(value: Mapping[str, Any]) -> str:
    if not isinstance(value, Mapping):
        raise ValueError("采集上下文必须是 JSON 对象")
    return json.dumps(dict(value), ensure_ascii=False, allow_nan=False, separators=(",", ":"))


def _recipe(value: SourceRecipe | None) -> str | None:
    if value is None:
        return None
    _text(value.adapter)
    if any(not isinstance(key, str) or not key.strip() for key in value.option_fields):
        raise ValueError("显式选项名无效")
    if any(
        not isinstance(key, str) or not key.strip() or type(flag) is not bool
        for key, flag in value.credentials.items()
    ):
        raise ValueError("采集凭据仅保存认证是否存在")
    return _json(asdict(value))


def _values(value: CaptureAttempt) -> dict[str, Any]:
    if value.status != "started" or any(
        item is not None for item in (value.finished_at, value.error_type, value.result)
    ):
        raise ValueError("采集必须从 started 状态开始")
    if value.cursor is not None and not isinstance(value.cursor, str):
        raise ValueError("原游标必须是字符串或空值")
    if value.scope.purpose not in ("discover", "inbox", "references", "survey"):
        raise ValueError("采集用途无效")
    return {
        "id": _text(value.id),
        "source": _text(value.source),
        "query": _json(value.query),
        "query_hash": _text(value.query_hash),
        "cursor": value.cursor,
        "purpose": value.scope.purpose,
        "owner": _json(value.scope.owner),
        "recipe": _recipe(value.recipe),
        "started_at": value.started_at,
        "started_us": _stamp(value.started_at),
        "status": "started",
    }


def _attempt(row: Row[Any]) -> CaptureAttempt:
    raw = json.loads(row.recipe) if row.recipe is not None else None
    recipe = (
        None
        if raw is None
        else SourceRecipe(
            raw["adapter"],
            raw["options"],
            tuple(raw["option_fields"]),
            raw["credentials"],
            raw["contact"],
        )
    )
    return CaptureAttempt(
        row.id,
        row.source,
        json.loads(row.query),
        row.query_hash,
        row.cursor,
        CaptureScope(cast(CapturePurpose, row.purpose), json.loads(row.owner)),
        recipe,
        row.started_at,
        cast(CaptureStatus, row.status),
        row.finished_at,
        row.error_type,
        json.loads(row.result) if row.result is not None else None,
    )


def _input(item: CapturedInput) -> dict[str, Any]:
    if any(
        type(value) is not int or not 0 <= value < 2**63 for value in (item.position, item.size)
    ):
        raise ValueError("输入位置和字节数必须是非负整数")
    for value in (item.attempt_id, item.kind, item.blob_key):
        _text(value)
    if not isinstance(item.sha256, str) or re.fullmatch(r"[0-9a-f]{64}", item.sha256) is None:
        raise ValueError("输入摘要必须是小写 SHA-256")
    return asdict(item)


def _finish(
    status: CaptureStatus, result: Mapping[str, Any] | None, error_type: str | None
) -> str | None:
    if status == "returned" and result is not None and error_type is None:
        return _json(result)
    if (
        status == "raised"
        and result is None
        and isinstance(error_type, str)
        and all(part.isidentifier() for part in error_type.split("."))
    ):
        return None
    raise ValueError("结束状态必须仅保存返回边界或错误类型")


class SqlCaptureRepo:
    def __init__(self, db: Database) -> None:
        self._db = db

    def start(self, attempt: CaptureAttempt) -> None:
        fields = _values(attempt)
        with self._db.atomic():
            if self._db.fetch(select(attempt_table.c.id).where(attempt_table.c.id == attempt.id)):
                raise ValueError("采集 ID 已存在，不能覆盖")
            self._db.write(insert(attempt_table).values(**fields))

    def append(self, item: CapturedInput) -> None:
        fields = _input(item)
        last = (
            select(func.max(input_table.c.position))
            .where(input_table.c.attempt_id == item.attempt_id)
            .scalar_subquery()
        )
        statement = select(
            attempt_table.c.status, func.coalesce(last, -1).label("last_position")
        ).where(attempt_table.c.id == item.attempt_id)
        with self._db.atomic():
            rows = self._db.fetch(statement)
            if (
                not rows
                or rows[0].status != "started"
                or item.position != rows[0].last_position + 1
            ):
                raise ValueError("输入必须按零基连续位置追加到 started 采集")
            self._db.write(insert(input_table).values(**fields))

    def finish(
        self,
        attempt_id: str,
        status: CaptureStatus,
        finished_at: str,
        *,
        result: Mapping[str, Any] | None = None,
        error_type: str | None = None,
    ) -> None:
        boundary = _finish(status, result, error_type)
        _stamp(finished_at)
        changed = self._db.write(
            update(attempt_table)
            .where(attempt_table.c.id == _text(attempt_id), attempt_table.c.status == "started")
            .values(status=status, finished_at=finished_at, result=boundary, error_type=error_type)
        )
        if changed != 1:
            raise ValueError("采集不存在或已结束")

    def get(self, attempt_id: str) -> CaptureAttempt | None:
        rows = self._db.fetch(select(attempt_table).where(attempt_table.c.id == attempt_id))
        return _attempt(rows[0]) if rows else None

    def inputs(self, attempt_id: str) -> tuple[CapturedInput, ...]:
        rows = self._db.fetch(
            select(input_table)
            .where(input_table.c.attempt_id == attempt_id)
            .order_by(input_table.c.position)
        )
        return tuple(
            CapturedInput(
                row.attempt_id, row.position, row.kind, row.blob_key, row.sha256, row.size
            )
            for row in rows
        )

    def list(
        self, source: str, since: str | None = None, *, limit: int = 100, after: str | None = None
    ) -> tuple[CaptureAttempt, ...]:
        if type(limit) is not int or not 1 <= limit <= 1000:
            raise ValueError("分页数量必须是 1 到 1000 的整数")
        statement = select(attempt_table).where(attempt_table.c.source == _text(source))
        if since is not None:
            statement = statement.where(attempt_table.c.started_us >= _stamp(since))
        if after is not None:
            statement = statement.where(attempt_table.c.id > _text(after))
        rows = self._db.fetch(statement.order_by(attempt_table.c.id).limit(limit))
        return tuple(_attempt(row) for row in rows)
