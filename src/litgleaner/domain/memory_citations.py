"""按唯一精确标识符归并引用目标，保留每个来源的独立证据。"""

from collections import defaultdict
from collections.abc import Iterator, Mapping
from dataclasses import dataclass

from litgleaner.domain.citation import Citation
from litgleaner.domain.identity import Alias
from litgleaner.domain.memory import ReferenceTarget

TargetKey = tuple[str, str, str]


@dataclass(frozen=True)
class CitationIndex:
    targets: Mapping[TargetKey, ReferenceTarget]
    by_paper: Mapping[str, Mapping[TargetKey, tuple[Citation, ...]]]
    direct: Mapping[tuple[str, str], tuple[Citation, ...]]


def _alias_key(alias: Alias) -> TargetKey:
    return "alias", alias.scheme, alias.value


def _root(parents: dict[TargetKey, TargetKey], key: TargetKey) -> TargetKey:
    parents.setdefault(key, key)
    while parents[key] != key:
        parents[key] = parents[parents[key]]
        key = parents[key]
    return key


def _link(parents: dict[TargetKey, TargetKey], keys: tuple[TargetKey, ...]) -> None:
    roots = tuple(_root(parents, key) for key in keys)
    first = min(roots)
    for root in roots:
        parents[root] = first


def _target_key(
    citation: Citation, aliases: Mapping[Alias, str]
) -> tuple[TargetKey, tuple[TargetKey, ...]] | None:
    keys = tuple(sorted({_alias_key(alias) for alias in citation.cited_aliases if alias.value}))
    hits = {aliases[alias] for alias in citation.cited_aliases if alias in aliases}
    if citation.cited_paper_id is not None:
        key = ("paper", "", citation.cited_paper_id)
        return key, (key,)  # 已保存的事实 ID 沿用；不借冲突别名合并其他目标。
    if len(hits) > 1 or not keys:
        return None
    key = ("paper", "", next(iter(hits))) if hits else keys[0]
    return key, (*keys, key)


def citation_index(
    citations: tuple[Citation, ...],
    paper_aliases: Mapping[str, tuple[Alias, ...]],
    titles: Mapping[str, str],
) -> CitationIndex:
    aliases = {alias: paper_id for paper_id, items in paper_aliases.items() for alias in items}
    parents: dict[TargetKey, TargetKey] = {}
    accepted: list[tuple[Citation, TargetKey]] = []
    for item in citations:
        matched = _target_key(item, aliases)
        if matched is not None:
            key, keys = matched
            _link(parents, keys)
            accepted.append((item, key))
    targets, by_paper = _indexed(accepted, parents, titles)
    return CitationIndex(targets, by_paper, _direct(citations, targets, by_paper))


def _resolved_groups(
    accepted: list[tuple[Citation, TargetKey]],
    parents: dict[TargetKey, TargetKey],
) -> Iterator[tuple[TargetKey, str | None, list[Citation]]]:
    groups: dict[TargetKey, list[Citation]] = defaultdict(list)
    for item, key in accepted:
        groups[_root(parents, key)].append(item)
    grouped_keys: dict[TargetKey, list[TargetKey]] = defaultdict(list)
    for candidate in parents:
        grouped_keys[_root(parents, candidate)].append(candidate)
    for key, items in sorted(groups.items()):
        keys = grouped_keys[key]
        ids = sorted(value for tag, _, value in keys if tag == "paper")
        if len(ids) > 1:
            # 动态链整体歧义不连边；显式ID仍是独立事实，不被该链擦除。
            explicit: dict[str, list[Citation]] = defaultdict(list)
            for item in items:
                if item.cited_paper_id is not None:
                    explicit[item.cited_paper_id].append(item)
            for paper_id, evidence in sorted(explicit.items()):
                yield ("paper", "", paper_id), paper_id, evidence
        else:
            yield key, ids[0] if ids else None, items


def _indexed(
    accepted: list[tuple[Citation, TargetKey]],
    parents: dict[TargetKey, TargetKey],
    titles: Mapping[str, str],
) -> tuple[dict[TargetKey, ReferenceTarget], dict[str, dict[TargetKey, tuple[Citation, ...]]]]:
    targets: dict[TargetKey, ReferenceTarget] = {}
    by_paper: dict[str, dict[TargetKey, tuple[Citation, ...]]] = defaultdict(dict)
    for key, paper_id, items in _resolved_groups(accepted, parents):
        target_aliases = tuple(
            sorted({a for item in items for a in item.cited_aliases}, key=_alias_key)
        )
        title = titles.get(paper_id or "") or next(
            (c.cited_title for c in items if c.cited_title), None
        )
        targets[key] = ReferenceTarget(paper_id, target_aliases, title)
        sources: dict[str, list[Citation]] = defaultdict(list)
        for item in items:
            sources[item.citing_paper_id].append(item)
        for paper, evidence in sources.items():
            by_paper[paper][key] = tuple(evidence)
    return targets, dict(by_paper)


def _direct(
    citations: tuple[Citation, ...],
    targets: Mapping[TargetKey, ReferenceTarget],
    by_paper: Mapping[str, Mapping[TargetKey, tuple[Citation, ...]]],
) -> dict[tuple[str, str], tuple[Citation, ...]]:
    resolved = {
        item: targets[key].paper_id
        for grouped in by_paper.values()
        for key, evidence in grouped.items()
        if targets[key].paper_id is not None
        for item in evidence
    }
    direct: dict[tuple[str, str], list[Citation]] = defaultdict(list)
    for item in citations:
        paper_id = resolved.get(item)
        if paper_id is not None:
            direct[item.citing_paper_id, paper_id].append(item)
    return {pair: tuple(evidence) for pair, evidence in direct.items()}
