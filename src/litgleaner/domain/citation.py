"""参考文献事实：保留原始条目与来源；匹配不到库内论文时保持未关联。"""

import re
from dataclasses import dataclass

from litgleaner.domain.identity import Alias, normalize_arxiv_id, normalize_doi
from litgleaner.domain.reading import BBox

_DOI = re.compile(r"(?<![a-z0-9])10\.\d{1,9}/[^\s<>\"']+", re.IGNORECASE)
_PREPRINT = re.compile(
    r"(?:arxiv\s*:\s*|https?://arxiv\.org/(?:abs|pdf)/)([^\s<>\"']+)", re.IGNORECASE
)


def _identifier_token(value: str) -> str:
    value = value.rstrip(".,;:!?")
    for left, right in (("(", ")"), ("[", "]"), ("{", "}")):
        while value.endswith(right) and value.count(right) > value.count(left):
            value = value[:-1].rstrip(".,;:!?")
    return value


def reference_aliases(text: str) -> tuple[Alias, ...]:
    """只识别原文显式标识符，不用标题相似度猜测参考文献。"""
    aliases: set[Alias] = set()
    for match in _DOI.findall(text):
        if value := normalize_doi(_identifier_token(match)):
            aliases.add(Alias("doi", value))
    for match in _PREPRINT.findall(text):
        if value := normalize_arxiv_id(_identifier_token(match)):
            aliases.add(Alias("arxiv", value))
    return tuple(sorted(aliases, key=lambda alias: (alias.scheme, alias.value)))


@dataclass(frozen=True)
class ReferenceRecord:
    citing_doi: str
    position: int
    raw_text: str
    cited_title: str | None
    cited_aliases: tuple[Alias, ...]
    contexts: tuple[str, ...] = ()
    intents: tuple[str, ...] = ()
    influential: bool | None = None


@dataclass(frozen=True)
class Citation:
    citing_paper_id: str
    position: str
    raw_text: str
    source: str
    source_position: int
    cited_paper_id: str | None
    cited_title: str | None
    cited_aliases: tuple[Alias, ...]
    contexts: tuple[str, ...]
    intents: tuple[str, ...]
    influential: bool | None
    created_at: str
    page: int | None = None
    bbox: BBox | None = None
