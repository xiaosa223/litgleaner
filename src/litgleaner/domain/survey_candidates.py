"""内存候选归并；只用显式标识符或缺标识符时的规范标题，不写事实库。"""

from collections.abc import Iterable
from dataclasses import dataclass, field

from litgleaner.domain.hashing import stable_hash
from litgleaner.domain.identity import (
    Alias,
    ExistingPaper,
    decide_resolution,
    normalize_arxiv_id,
    normalize_doi,
    record_aliases,
    title_key,
)
from litgleaner.domain.paper import SourceRecord
from litgleaner.domain.survey import SurveyCandidate, SurveyMode


def candidate_key(record: SourceRecord) -> str:
    if doi := normalize_doi(record.doi):
        return f"doi:{doi}"
    if preprint := normalize_arxiv_id(record.arxiv_id):
        return f"arxiv:{preprint}"
    return f"title:{title_key(record.title)}:{stable_hash(record.authors)[:16]}"


def _strong(record: SourceRecord) -> set[Alias]:
    return {alias for alias in record_aliases(record) if alias.scheme in {"doi", "arxiv"}}


@dataclass
class _Candidate:
    record: SourceRecord
    aliases: set[Alias]
    sources: set[str] = field(default_factory=set)
    modes: set[SurveyMode] = field(default_factory=set)
    referenced_by: set[str] = field(default_factory=set)


class CandidatePool:
    def __init__(self) -> None:
        self._items: list[_Candidate] = []

    def _matches(self, record: SourceRecord) -> list[_Candidate]:
        aliases = _strong(record)
        exact = [item for item in self._items if aliases & item.aliases]
        if exact:
            # 同一条记录桥接多个既有候选是标识符冲突，保留独立记录供后续处理。
            return exact if len(exact) == 1 else []
        same_title = [
            item for item in self._items if title_key(item.record.title) == title_key(record.title)
        ]
        title_matches = [
            item
            for item in same_title
            if (not aliases or not _strong(item.record))
            and decide_resolution(
                record, frozenset(), (ExistingPaper("candidate", item.record.authors),)
            ).kind
            == "existing"
        ]
        return title_matches if len(title_matches) == 1 else []

    def aliases(self, record: SourceRecord) -> set[Alias]:
        return set(record_aliases(record)) | {
            alias for item in self._matches(record) for alias in item.aliases
        }

    def add(
        self, record: SourceRecord, source: str, mode: SurveyMode, referenced_by: Iterable[str] = ()
    ) -> None:
        matches = self._matches(record)
        if matches:
            item = matches[0]
            self._merge(item, _Candidate(record, set(record_aliases(record))))
        else:
            item = _Candidate(record, set(record_aliases(record)))
            self._items.append(item)
        item.sources.add(source)
        item.modes.add(mode)
        item.referenced_by.update(referenced_by)

    @staticmethod
    def _merge(item: _Candidate, other: _Candidate) -> None:
        values = item.record.model_dump()
        for name, value in other.record.model_dump().items():
            if values[name] is None or values[name] == () or values[name] == {}:
                values[name] = value
        values["external_ids"] = {**other.record.external_ids, **item.record.external_ids}
        item.record = SourceRecord.model_validate(values)
        item.aliases.update(other.aliases)
        item.sources.update(other.sources)
        item.modes.update(other.modes)
        item.referenced_by.update(other.referenced_by)

    def candidates(self) -> tuple[SurveyCandidate, ...]:
        seen: dict[str, int] = {}
        items = (
            SurveyCandidate(
                key=self._unique_key(item.record, seen),
                record=item.record,
                sources=tuple(sorted(item.sources)),
                modes=tuple(sorted(item.modes)),
                referenced_by=tuple(sorted(item.referenced_by)),
            )
            for item in self._items
        )
        return tuple(sorted(items, key=lambda item: (-len(item.referenced_by), item.key)))

    @staticmethod
    def _unique_key(record: SourceRecord, seen: dict[str, int]) -> str:
        key = candidate_key(record)
        ordinal = seen.get(key, 0)
        seen[key] = ordinal + 1
        return f"{key}:{ordinal}" if ordinal else key
