"""按唯一精确标识符归并引用目标，保留每个来源的独立证据。"""

from collections import defaultdict
from collections.abc import Mapping
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
    direct: dict[tuple[str, str], list[Citation]] = defaultdict(list)
    for item in citations:
        matched = _target_key(item, aliases)
        if matched is not None:
            key, keys = matched
            _link(parents, keys)
            accepted.append((item, key))
            if key[0] == "paper":
                direct[item.citing_paper_id, key[2]].append(item)
    targets, by_paper = _indexed(accepted, parents, titles)
    return CitationIndex(targets, by_paper, {key: tuple(items) for key, items in direct.items()})


def _indexed(
    accepted: list[tuple[Citation, TargetKey]],
    parents: dict[TargetKey, TargetKey],
    titles: Mapping[str, str],
) -> tuple[dict[TargetKey, ReferenceTarget], dict[str, dict[TargetKey, tuple[Citation, ...]]]]:
    groups: dict[TargetKey, list[Citation]] = defaultdict(list)
    for item, key in accepted:
        groups[_root(parents, key)].append(item)
    targets: dict[TargetKey, ReferenceTarget] = {}
    by_paper: dict[str, dict[TargetKey, tuple[Citation, ...]]] = defaultdict(dict)
    grouped_keys: dict[TargetKey, list[TargetKey]] = defaultdict(list)
    for candidate in parents:
        grouped_keys[_root(parents, candidate)].append(candidate)
    for key, items in sorted(groups.items()):
        keys = grouped_keys[key]
        ids = sorted(value for tag, _, value in keys if tag == "paper")
        if len(ids) > 1:
            continue  # 跨条目别名链产生歧义，同样不参与关系。
        paper_id = ids[0] if ids else None
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
