"""主题文件、术语表和检索式草稿的写入（第 7.1、7.2 节）。

格式与手写的文件相同，命令行和领读器编辑的是同一份文件。全部先写临时文件再落位，不留下写了一半的文件。
"""

import logging
import os
import tempfile
from pathlib import Path

import yaml
from pydantic import ValidationError

from litgleaner.domain.errors import ConfigError, InvalidRequest
from litgleaner.domain.glossary import Glossary
from litgleaner.domain.topic import Topic
from litgleaner.domain.topic_draft import TopicDraft

logger = logging.getLogger(__name__)


def _dump(data: object) -> bytes:
    return yaml.safe_dump(data, allow_unicode=True, sort_keys=False).encode("utf-8")


def topic_yaml(topic: Topic) -> bytes:
    return _dump(topic.model_dump(mode="json"))


_MODE = 0o644  # 与手写的配置文件一样可读；mkstemp 默认只有本人可读
_EXCLUSIVE = os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_BINARY", 0)


def _temporary(directory: Path, name: str, content: bytes) -> str:
    descriptor, temporary = tempfile.mkstemp(prefix=f".{name}.", dir=directory)
    with os.fdopen(descriptor, "wb") as handle:
        handle.write(content)
        handle.flush()
        os.fsync(handle.fileno())
    os.chmod(temporary, _MODE)
    return temporary


def _write_new(path: Path, content: bytes) -> None:
    """不支持硬链接的盘（exFAT、部分网络盘）上退回独占创建；写失败时删掉半个文件。"""
    descriptor = os.open(path, _EXCLUSIVE, _MODE)
    try:
        with os.fdopen(descriptor, "wb") as handle:
            handle.write(content)
            handle.flush()
            os.fsync(handle.fileno())
    except OSError:
        path.unlink(missing_ok=True)
        raise


def _replace(path: Path, content: bytes) -> None:
    temporary = _temporary(path.parent, path.name, content)
    try:
        os.replace(temporary, path)
    finally:
        Path(temporary).unlink(missing_ok=True)


def _read_topic(path: Path) -> Topic | None:
    try:
        return Topic.model_validate(yaml.safe_load(path.read_text(encoding="utf-8")))
    except (OSError, yaml.YAMLError, ValidationError):
        return None


class TopicFiles:
    def __init__(self, directory: Path) -> None:
        self.directory = directory

    def create(self, topic: Topic) -> None:
        """以硬链接落位：已有同名文件时失败，不覆盖。"""
        path = self.directory / f"{topic.id}.yaml"
        try:
            self.directory.mkdir(parents=True, exist_ok=True)
            temporary = _temporary(self.directory, topic.id, topic_yaml(topic))
        except OSError:
            raise ConfigError("topic_save_failed") from None
        try:
            try:
                os.link(temporary, path)
            except FileExistsError:
                raise
            except OSError:
                _write_new(path, topic_yaml(topic))
        except FileExistsError:
            raise InvalidRequest("topic_exists") from None
        except OSError:
            raise ConfigError("topic_save_failed") from None
        finally:
            Path(temporary).unlink(missing_ok=True)

    def update(self, before: Topic, after: Topic, glossary: Glossary) -> None:
        """主题文件被手改过时拒绝。主题文件和术语表各留一份 .bak；主题文件写失败时术语表还原。"""
        path = self.directory / f"{before.id}.yaml"
        terms = self.directory / f"{before.id}.glossary.yaml"
        if _read_topic(path) != before:
            raise InvalidRequest("topic_changed_on_disk")
        try:
            old_terms = terms.read_bytes() if terms.exists() else None
            _replace(path.with_suffix(".yaml.bak"), path.read_bytes())
            if old_terms is not None:
                _replace(terms.with_suffix(".yaml.bak"), old_terms)
            _replace(terms, _dump(glossary.model_dump(mode="json")))
        except OSError:
            raise ConfigError("topic_save_failed") from None
        try:
            _replace(path, topic_yaml(after))
        except OSError:
            self._restore(terms, old_terms)
            raise ConfigError("topic_save_failed") from None

    @staticmethod
    def _restore(path: Path, content: bytes | None) -> None:
        try:
            if content is None:
                path.unlink(missing_ok=True)
            else:
                _replace(path, content)
        except OSError:
            logger.exception("术语表 %s 没能还原，原内容在 .bak 里", path.name)


class DraftFiles:
    def __init__(self, directory: Path) -> None:
        self.directory = directory

    def _path(self, topic_id: str) -> Path:
        return self.directory / f"{topic_id}.draft.yaml"

    def save(self, draft: TopicDraft) -> None:
        self.directory.mkdir(parents=True, exist_ok=True)
        _replace(self._path(draft.topic_id), _dump(draft.model_dump(mode="json")))

    def load(self, topic_id: str) -> TopicDraft | None:
        path = self._path(topic_id)
        if not path.is_file():
            return None
        try:
            return TopicDraft.model_validate(yaml.safe_load(path.read_text(encoding="utf-8")))
        except (OSError, yaml.YAMLError, ValidationError):
            logger.warning("检索式草稿 %s 读不出来，当作没有草稿", path.name)
            return None
