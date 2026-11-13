"""新建研究画像：编号、默认值与输入校验（第 7.1 节），不调用模型。"""

import pytest

from litgleaner.application.topic_setup import define_topic, revise_topic, topic_id_for
from litgleaner.domain.errors import ConfigError, InvalidRequest
from litgleaner.ports.topic_settings import GoalText, TopicFields, TopicProfile

RUBRICS = {"default": object(), "other": object()}


def fields(**changes):
    data = {
        "name": "EEG foundation models",
        "background": "背景",
        "content": "内容",
        "goals": ("目标一", "目标二"),
    }
    return TopicFields(**{**data, **changes})


@pytest.mark.parametrize(
    ("name", "taken", "expected"),
    [
        ("EEG Foundation Models!", set(), "eeg-foundation-models"),
        ("脑电基础模型", set(), "topic"),
        ("脑电 EEG 模型", set(), "topic"),  # 混排时取英文片段会误导，统一用 topic
        ("EEG", {"eeg", "eeg-2"}, "eeg-3"),
        ("con", set(), "topic-con"),  # Windows 保留的文件名
        ("a" * 80, set(), "a" * 40),
    ],
)
def test_topic_id_is_a_safe_unique_file_stem(name, taken, expected):
    assert topic_id_for(name, taken) == expected


def test_define_topic_numbers_goals_and_uses_default_rubric_without_query():
    topic = define_topic(fields(), {"eeg-foundation-models"}, RUBRICS)
    assert topic.id == "eeg-foundation-models-2"
    assert [(goal.id, goal.text) for goal in topic.goals] == [("g1", "目标一"), ("g2", "目标二")]
    assert topic.rubric == "default"
    assert topic.query.terms == () and topic.journal_sets == () and not topic.public


def test_define_topic_strips_text_and_drops_blank_goals():
    profile = fields(name="  X  ", background=" b ", goals=(" a ", "  "))
    topic = define_topic(profile, set(), RUBRICS)
    assert (topic.name, topic.background, [goal.text for goal in topic.goals]) == ("X", "b", ["a"])


def test_define_topic_falls_back_to_the_first_rubric():
    assert define_topic(fields(), set(), {"zeta": 1, "beta": 2}).rubric == "beta"


def test_define_topic_without_rubrics_is_a_configuration_error():
    with pytest.raises(ConfigError):
        define_topic(fields(), set(), {})


@pytest.mark.parametrize(
    "change",
    [
        {"name": " "},
        {"background": ""},
        {"content": "  "},
        {"goals": (" ",)},
        {"goals": tuple(f"g{i}" for i in range(21))},
        {"name": "x" * 101},
    ],
)
def test_define_topic_rejects_incomplete_or_oversized_profiles(change):
    with pytest.raises(InvalidRequest):
        define_topic(fields(**change), set(), RUBRICS)


def _topic():
    return define_topic(
        TopicFields("Alpha", "背景", "内容", ("目标一", "目标二", "目标三")), (), {"default": 1}
    )


def test_revise_keeps_goal_ids_and_never_reuses_a_deleted_one():
    before = _topic()
    profile = TopicProfile(
        " Alpha 2 ",
        " 新背景 ",
        "内容",
        (GoalText("g1", " 目标一改 "), GoalText(None, "新目标"), GoalText(None, " ")),
    )
    after = revise_topic(before, profile)
    assert after.id == before.id and after.name == "Alpha 2" and after.background == "新背景"
    assert [(g.id, g.text) for g in after.goals] == [("g1", "目标一改"), ("g4", "新目标")]
    assert after.query == before.query and after.rubric == before.rubric


@pytest.mark.parametrize(
    "goals",
    [
        (GoalText("g9", "不存在的编号"),),
        (GoalText("g1", "一"), GoalText("g1", "重复")),
        (GoalText(None, " "),),
    ],
)
def test_revise_rejects_unknown_duplicate_or_missing_goals(goals):
    with pytest.raises(InvalidRequest):
        revise_topic(_topic(), TopicProfile("Alpha", "背景", "内容", goals))


def test_revise_rejects_blank_text():
    with pytest.raises(InvalidRequest, match="topic_incomplete"):
        revise_topic(_topic(), TopicProfile("Alpha", " ", "内容", (GoalText("g1", "一"),)))
