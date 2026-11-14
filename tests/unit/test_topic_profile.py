"""从上传文档推断研究画像（ADR 0012）：先确认模型路由，文字截断，输出整理成可填表的字段。"""

from unittest.mock import Mock

import pytest

from litgleaner.application.topic_profile import PROFILE_TOKEN_BUDGET, infer_profile
from litgleaner.domain.errors import InvalidRequest, ModelConfigurationRequired
from litgleaner.ports.document_text import DocumentText

ANSWER = {
    "name": "  脑电基础模型的小样本迁移  ",
    "background": "背景",
    "content": "内容",
    "goals": ["目标一", " ", "目标二", "目标三", "目标四", "目标五", "目标六"],
}


def _ctx(text: str = "任务书正文", answer: dict | None = None, *, routed: bool = True):
    documents = Mock(spec=DocumentText)
    documents.extract.return_value = text
    llm = Mock()
    llm.call.return_value = Mock(data=answer or ANSWER)
    if not routed:
        llm.require_route.side_effect = ModelConfigurationRequired("profile")
    settings = Mock(reading_language="zh-CN")
    ctx = Mock(documents=documents, llm=llm, settings=settings)
    return ctx


def test_profile_is_inferred_from_extracted_text_as_untrusted_input():
    ctx = _ctx()
    guess = infer_profile(ctx, "任务书.pdf", b"%PDF-")
    ctx.documents.extract.assert_called_once_with("任务书.pdf", b"%PDF-")
    task, variables, untrusted, schema = ctx.llm.call.call_args.args
    assert task == "profile"
    assert "output_language" in variables
    assert untrusted == "任务书正文"
    assert schema["required"] == ["name", "background", "content", "goals"]
    assert guess.name == "脑电基础模型的小样本迁移"
    assert guess.goals == ("目标一", "目标二", "目标三", "目标四", "目标五")


def test_missing_model_is_reported_before_reading_the_document():
    ctx = _ctx(routed=False)
    with pytest.raises(ModelConfigurationRequired):
        infer_profile(ctx, "a.pdf", b"x")
    ctx.documents.extract.assert_not_called()
    ctx.llm.call.assert_not_called()


def test_long_documents_are_truncated_to_the_token_budget():
    ctx = _ctx(text="词" * (PROFILE_TOKEN_BUDGET * 10))
    infer_profile(ctx, "long.txt", b"x")
    untrusted = ctx.llm.call.call_args.args[2]
    assert len(untrusted) < PROFILE_TOKEN_BUDGET * 10
    assert untrusted.endswith("（后文已截断）")


def test_unavailable_extractor_is_a_fixed_error():
    ctx = _ctx()
    ctx.documents = None
    with pytest.raises(InvalidRequest, match="document_unavailable"):
        infer_profile(ctx, "a.pdf", b"x")


def test_fields_are_clipped_to_topic_limits():
    long_answer = {**ANSWER, "name": "名" * 300, "background": "背" * 9000}
    guess = infer_profile(_ctx(answer=long_answer), "a.txt", b"x")
    assert len(guess.name) <= 100
    assert len(guess.background) <= 8000
