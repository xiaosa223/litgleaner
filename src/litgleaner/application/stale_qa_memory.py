"""新问答按答案自带的冻结来源核验，任务清理不损失出处。"""

from litgleaner.application.qa_memory_state import check
from litgleaner.application.stale_inputs import StaleInputs
from litgleaner.domain.hashing import stable_hash
from litgleaner.domain.qa import Answer, Question
from litgleaner.domain.stale import StaleItem, item


def answer_item(basis: StaleInputs, answer: Answer, question: Question | None) -> StaleItem:
    reasons: list[str] = []
    unchecked: list[str] = []
    if question is None:
        unchecked.append("缺少回答对应的问题记录")
    else:
        known, unknown = check(basis, question, answer.context)
        reasons.extend(known)
        unchecked.extend(unknown)
        if answer.paper_id != question.paper_id:
            reasons.append("回答与问题所属论文不一致")
    if not answer.request_producer_version:
        unchecked.append("回答未保存完整请求生产者")
    elif answer.request_producer_version != answer.context.get("producer"):
        reasons.append("回答生产者与冻结请求不一致")
    if answer.input_hash != stable_hash(answer.context.get("inputs")):
        reasons.append("回答输入与冻结消息不一致")
    if answer.producer_version != answer.context.get("prompt"):
        reasons.append("回答提示词与冻结请求不一致")
    return item(
        "answer",
        answer.id,
        answer.paper_id,
        None,
        reasons=reasons,
        unchecked=unchecked,
        selected=True,
    )
