"""历史重提取的窄快照及可复算规范元数据指纹，不读取旧提取正文。"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import asdict, dataclass
from typing import TYPE_CHECKING, Protocol

from litgleaner.domain.hashing import stable_hash
from litgleaner.domain.paper import Paper

if TYPE_CHECKING:
    from litgleaner.ports.store import FullTextState, Job


@dataclass(frozen=True)
class ReextractFingerprint:
    id: int
    paper_id: str
    input_hash: str
    extractor_version: str


@dataclass(frozen=True)
class ReextractSnapshot:
    papers: tuple[Paper, ...]
    fulltexts: Mapping[str, FullTextState]
    extractions: tuple[ReextractFingerprint, ...]
    jobs: tuple[Job, ...]
    revision: str


def metadata_revision(
    papers: tuple[Paper, ...],
    fulltexts: Mapping[str, FullTextState],
    extractions: tuple[ReextractFingerprint, ...],
) -> str:
    """任务状态不属于来源；应用可筛选单篇后复算相同的指纹。"""
    return stable_hash(
        {
            "papers": [asdict(item) for item in sorted(papers, key=lambda item: item.id)],
            "fulltexts": {key: asdict(fulltexts[key]) for key in sorted(fulltexts)},
            "extractions": [asdict(item) for item in sorted(extractions, key=lambda item: item.id)],
        }
    )


class ReextractStore(Protocol):
    def reextract_snapshot(self, paper_ids: tuple[str, ...] | None = None) -> ReextractSnapshot: ...
    def reextract_revision(self, paper_ids: tuple[str, ...] | None = None) -> str: ...
