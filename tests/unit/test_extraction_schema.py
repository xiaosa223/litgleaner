"""提取 v2 的模型输出协议；可信核验字段由代码补充。"""

from copy import deepcopy

import pytest
from jsonschema import Draft202012Validator, ValidationError

from litgleaner.adapters.fakes.llm import FakeLLM
from litgleaner.application.extraction_schema import EXTRACT_SCHEMA, EXTRACTION_CONTRACT_VERSION
from litgleaner.ports.llm import LLMRequest

FIELDS = (
    "summary",
    "conclusion",
    "innovation",
    "key_concepts",
    "limitations",
    "problem",
    "prior_gap",
    "approach",
    "method",
    "claims",
    "datasets",
    "metrics",
    "results",
    "code_data",
    "questions",
    "highlights",
)


def evidence():
    return {"text": "方法和结论仅适用于本文实验条件。", "quote": "We found it works.", "page": 1}


def valid_data():
    return {
        "summary": "研究总结",
        "conclusion": "研究结论",
        "innovation": None,
        "key_concepts": [{"name": "Method", "role": "core", "definition": None, "page": 0}],
        "limitations": [
            {"text": "作者自述限制", "source": "author", "quote": "We found it works.", "page": 1}
        ],
        "problem": evidence(),
        "prior_gap": None,
        "approach": evidence(),
        "method": evidence(),
        "claims": [evidence()],
        "datasets": [evidence()],
        "metrics": [evidence()],
        "results": [evidence()],
        "code_data": [
            {
                "kind": "code",
                "availability": "public",
                "text": "作者提及代码",
                "url": "https://example.test/code",
                "quote": "We found it works.",
                "page": 1,
            }
        ],
        "questions": [
            {"question": text, "answer": evidence() if i == 0 else None}
            for i, text in enumerate(("为何研究？", "如何研究？", "结论边界？"))
        ],
        "highlights": [{"kind": "method", "quote": "We found it works.", "page": 1}],
    }


def validate(data):
    Draft202012Validator(EXTRACT_SCHEMA).validate(data)


def test_valid_v2_schema_accepts_complete_protocol_and_checks_its_own_schema():
    Draft202012Validator.check_schema(EXTRACT_SCHEMA)
    assert EXTRACTION_CONTRACT_VERSION == "v2"
    validate(valid_data())


def test_evidence_absence_is_explicit_and_does_not_require_invented_facts():
    data = valid_data()
    for field in ("problem", "prior_gap", "approach", "method"):
        data[field] = None
    for field in ("claims", "datasets", "metrics", "results", "code_data", "highlights"):
        data[field] = []
    data["limitations"] = [
        {"text": "基于正文的模型推断", "source": "inferred", "quote": None, "page": None}
    ]
    data["questions"] = [{"question": f"问题 {i}", "answer": None} for i in range(3)]
    validate(data)


@pytest.mark.parametrize("field", FIELDS)
def test_every_contract_field_is_required_even_when_its_value_can_be_unknown(field):
    data = valid_data()
    del data[field]
    with pytest.raises(ValidationError):
        validate(data)


@pytest.mark.parametrize("field", ["problem", "prior_gap", "approach", "method"])
@pytest.mark.parametrize("extra", ["verified", "bbox"])
def test_model_cannot_supply_trusted_fields_inside_any_evidence(field, extra):
    data = valid_data()
    data[field] = {**evidence(), extra: True if extra == "verified" else [0, 0, 10, 10]}
    with pytest.raises(ValidationError):
        validate(data)


@pytest.mark.parametrize(
    ("key", "value"),
    [
        ("text", ""),
        ("text", "   "),
        ("text", "x" * 801),
        ("quote", ""),
        ("quote", "\n\t"),
        ("quote", "x" * 2001),
        ("page", -1),
        ("page", True),
        ("page", 1.5),
        ("page", "1"),
        ("page", None),
    ],
)
def test_evidence_requires_nonblank_bounded_text_quote_and_nonnegative_integer_page(key, value):
    data = valid_data()
    data["claims"][0][key] = value
    with pytest.raises(ValidationError):
        validate(data)


@pytest.mark.parametrize("field", ["claims", "datasets", "metrics", "results"])
def test_evidence_arrays_require_complete_items_and_are_bounded(field):
    data = valid_data()
    data[field] = [{"text": "Unsupported claim", "quote": "A quote"}]
    with pytest.raises(ValidationError):
        validate(data)
    data[field] = [evidence()] * 1001
    with pytest.raises(ValidationError):
        validate(data)


@pytest.mark.parametrize("count", [0, 1, 2, 4])
def test_questions_require_exactly_three_entries(count):
    data = valid_data()
    data["questions"] = [{"question": f"问题 {i}", "answer": None} for i in range(count)]
    with pytest.raises(ValidationError):
        validate(data)


@pytest.mark.parametrize(
    "question",
    [
        {"question": "缺少answer"},
        {"question": " ", "answer": None},
        {"question": "有问题", "answer": {"text": "无引用"}},
        {"question": "有问题", "answer": None, "verified": True},
    ],
)
def test_questions_cannot_omit_answers_or_forge_supported_answer_fields(question):
    data = valid_data()
    data["questions"][0] = question
    with pytest.raises(ValidationError):
        validate(data)


@pytest.mark.parametrize("source", ["author", "inferred"])
def test_limitations_with_a_quote_require_a_real_page_field(source):
    data = valid_data()
    data["limitations"][0]["source"] = source
    for value in (None, -1, True, "1"):
        data["limitations"][0]["page"] = value
        with pytest.raises(ValidationError):
            validate(data)
    data["limitations"][0]["page"] = 0
    validate(data)


def test_author_limitations_cannot_be_claimed_without_quote_and_page():
    data = valid_data()
    data["limitations"][0].update(quote=None, page=None)
    with pytest.raises(ValidationError):
        validate(data)
    data["limitations"][0]["source"] = "inferred"
    validate(data)
    data["limitations"][0]["page"] = 1
    with pytest.raises(ValidationError):
        validate(data)


@pytest.mark.parametrize("field", ["quote", "page"])
def test_limitations_evidence_fields_cannot_be_omitted(field):
    data = valid_data()
    del data["limitations"][0][field]
    with pytest.raises(ValidationError):
        validate(data)


@pytest.mark.parametrize("field", ["kind", "availability", "text", "url", "quote", "page"])
def test_code_data_has_explicit_fields_even_when_values_are_unknown(field):
    data = valid_data()
    del data["code_data"][0][field]
    with pytest.raises(ValidationError):
        validate(data)


def test_unknown_code_data_does_not_mean_not_public_and_does_not_require_url():
    data = valid_data()
    data["code_data"][0].update(availability="unknown", url=None, quote=None, page=None)
    validate(data)
    data["code_data"][0].update(availability="public")
    validate(data)
    data["code_data"][0].update(availability="private")
    with pytest.raises(ValidationError):
        validate(data)


@pytest.mark.parametrize(
    "item",
    [
        {"kind": "invented", "quote": "Original text", "page": 0},
        {"kind": "result", "quote": " ", "page": 0},
        {"kind": "goal", "quote": "Original text", "page": -1},
        {"kind": "innovation", "quote": "Original text", "page": 0, "bbox": [0, 0, 10, 10]},
        {"kind": "method", "quote": "Original text", "page": 0, "verified": True},
    ],
)
def test_highlights_only_accept_original_quote_kind_and_page_without_trust_claims(item):
    data = valid_data()
    data["highlights"] = [item]
    with pytest.raises(ValidationError):
        validate(data)


@pytest.mark.parametrize("location", ["root", "key_concepts", "limitations", "code_data"])
def test_all_output_objects_reject_model_generated_extra_properties(location):
    data = valid_data()
    target = data if location == "root" else data[location][0]
    target["verified"] = True
    with pytest.raises(ValidationError):
        validate(data)


def test_fake_default_obeys_v2_without_inventing_new_evidence_and_preserves_legacy_summary():
    request = LLMRequest("extract", "fake", "instructions", "We found it works.", EXTRACT_SCHEMA)
    data = FakeLLM().complete(request).data
    validate(data)
    assert data["summary"] == "演示总结" and data["conclusion"] == "演示结论"
    assert data["innovation"] == "演示创新点"
    assert data["limitations"] == [
        {"text": "演示不足", "source": "author", "quote": "We found it works.", "page": 1}
    ]
    assert data["claims"] == [] and data["code_data"] == []
    assert len(data["questions"]) == 3 and all(q["answer"] is None for q in data["questions"])


def test_fake_override_is_not_silently_repaired_to_hide_bad_protocol():
    fake = FakeLLM()
    override = deepcopy(valid_data())
    del override["claims"]
    fake.overrides["extract"] = override
    data = fake.complete(LLMRequest("extract", "fake", "instructions", "body", EXTRACT_SCHEMA)).data
    assert data == override
    with pytest.raises(ValidationError):
        validate(data)
