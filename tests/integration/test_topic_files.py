"""主题文件写入：只新建、不覆盖，写出的文件能被原加载器读回（第 7.1 节）。"""

import os

import pytest

from litgleaner.domain.errors import InvalidRequest
from litgleaner.domain.topic import Topic
from litgleaner.infra.config import _load_topics
from litgleaner.infra.topic_files import TopicFiles

TOPIC = Topic(
    id="eeg",
    name="脑电基础模型",
    background="背景：多行\n第二行",
    content="内容",
    goals=({"id": "g1", "text": "目标"},),
    rubric="default",
)


def test_created_file_round_trips_through_the_loader(tmp_path):
    directory = tmp_path / "topics"
    TopicFiles(directory).create(TOPIC)
    assert _load_topics(directory) == {"eeg": TOPIC}
    assert "脑电基础模型" in (directory / "eeg.yaml").read_text(encoding="utf-8")
    assert [path.name for path in directory.iterdir()] == ["eeg.yaml"]


def test_existing_topic_is_never_overwritten(tmp_path):
    directory = tmp_path / "topics"
    directory.mkdir()
    (directory / "eeg.yaml").write_text("keep", encoding="utf-8")
    with pytest.raises(InvalidRequest):
        TopicFiles(directory).create(TOPIC)
    assert (directory / "eeg.yaml").read_text(encoding="utf-8") == "keep"
    assert [path.name for path in directory.iterdir()] == ["eeg.yaml"]


def test_without_hard_links_creation_still_refuses_to_overwrite(tmp_path, monkeypatch):
    def no_links(*_args):
        raise PermissionError("hard links not supported")

    monkeypatch.setattr(os, "link", no_links)
    directory = tmp_path / "topics"
    TopicFiles(directory).create(TOPIC)
    assert _load_topics(directory) == {"eeg": TOPIC}
    with pytest.raises(InvalidRequest):
        TopicFiles(directory).create(TOPIC)
    assert [path.name for path in directory.iterdir()] == ["eeg.yaml"]


@pytest.mark.skipif(os.name == "nt", reason="Windows 不用 POSIX 权限位")
def test_written_files_are_readable_like_hand_written_ones(tmp_path):
    TopicFiles(tmp_path).create(TOPIC)
    assert (tmp_path / "eeg.yaml").stat().st_mode & 0o777 == 0o644
