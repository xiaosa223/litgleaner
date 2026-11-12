"""关系反馈追加和状态变更原子提交，请求ID不能换目标或动作重用。"""

from ulid import ULID

from litgleaner.application.context import AppContext
from litgleaner.domain.errors import InvalidRequest, NotFound
from litgleaner.domain.hashing import stable_hash
from litgleaner.domain.paper_relation import PaperRelation, RelationAction, RelationKind
from litgleaner.ports.feedback import FeedbackRecord


def apply(
    ctx: AppContext,
    relation_id: str,
    action: RelationAction,
    request_id: str,
    *,
    kind: RelationKind | None = None,
) -> PaperRelation:
    if not isinstance(request_id, str) or not request_id.strip() or len(request_id) > 200:
        raise InvalidRequest("关系反馈需要1–200字符的请求ID")
    if (action == "change") != (kind is not None):
        raise InvalidRequest("改类型必须指定关系类型；确认和删除不接受新类型")
    with ctx.store.atomic():
        current = ctx.store.relations.get(relation_id)
        if current is None:
            raise NotFound("论文关系不存在")
        record = FeedbackRecord(
            str(ULID()),
            current.paper_a,
            "reader",
            action,
            external_id=request_id,
            relation_id=relation_id,
            payload={"kind": kind},
        )
        old = ctx.store.feedback.by_external_id(request_id)
        if old is not None:
            return _replay(ctx, old, record)
        if action == "change":
            ids = {paper.id for paper in ctx.store.memory_snapshot().papers}
            if not {current.paper_a, current.paper_b} <= ids:
                raise InvalidRequest("改类型需要两篇论文仍在文库；历史确认和删除仍可使用")
        ctx.store.feedback.add(record, ctx.now_iso())
        return ctx.store.relations.apply_feedback(
            relation_id, action, record.id, kind, ctx.now_iso()
        )


def _replay(ctx: AppContext, old: FeedbackRecord, expected: FeedbackRecord) -> PaperRelation:
    fields = ("paper_id", "channel", "action", "relation_id", "payload")
    if any(getattr(old, name) != getattr(expected, name) for name in fields):
        raise InvalidRequest("反馈请求ID已用于其他目标或动作")
    id_ = (
        stable_hash({"feedback": old.id, "relation": old.relation_id})
        if old.action == "change"
        else old.relation_id
    )
    result = ctx.store.relations.get(id_ or "")
    if result is None:
        raise InvalidRequest("反馈结果缺失，请检查数据库完整性")
    return result
