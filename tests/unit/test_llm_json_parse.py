import pytest

from litgleaner.adapters.llm.base import parse_json_object
from litgleaner.domain.errors import TransientError


def test_plain_and_fenced_objects() -> None:
    assert parse_json_object('{"a": 1}', "s") == {"a": 1}
    assert parse_json_object('```json\n{"a": 1}\n```', "s") == {"a": 1}


def test_stray_markers_around_single_object_are_skipped() -> None:
    assert parse_json_object('<escape>{\n "a": "x{y}"\n}<escape>', "s") == {"a": "x{y}"}
    assert parse_json_object('好的，结果如下：{"a": [1, 2]}', "s") == {"a": [1, 2]}


@pytest.mark.parametrize(
    "text",
    ["", "not json", '{"a": 1} {"b": 2}', "[1, 2]", '<x>{"a": 1', '[{"x": 1}]', 'x [ {"x": 1}'],
    ids=str,
)
def test_rejects_missing_truncated_or_ambiguous(text: str) -> None:
    with pytest.raises(TransientError):
        parse_json_object(text, "s")
