"""只用已保存元数据生成开放引用格式；标识符缺失或歧义时不猜测。"""

import hashlib
import json
import unicodedata
from dataclasses import dataclass
from datetime import date
from urllib.parse import quote

from litgleaner.domain.identity import Alias, normalize_arxiv_id, normalize_doi
from litgleaner.domain.paper import Paper

ReferenceInput = tuple[tuple[Paper, tuple[Alias, ...]], ...]
_GENERIC_TYPE = ("article", "misc")
_REFERENCE_TYPES = {
    "book": ("book", "book"),
    "bookchapter": ("chapter", "incollection"),
    "booksection": ("chapter", "incollection"),
    "chapter": ("chapter", "incollection"),
    "journalarticle": ("article-journal", "article"),
    "proceedingsarticle": ("paper-conference", "inproceedings"),
    "conferencepaper": ("paper-conference", "inproceedings"),
    "conference": ("paper-conference", "inproceedings"),
}
_TEX = {
    "\\": r"\textbackslash{}",
    "{": r"\textbraceleft{}",
    "}": r"\textbraceright{}",
    "%": r"\%",
    "#": r"\#",
    "&": r"\&",
    "_": r"\_",
    "$": r"\$",
    "^": r"\textasciicircum{}",
    "~": r"\textasciitilde{}",
}


@dataclass(frozen=True)
class ReferenceExport:
    bibtex: str
    csl_json: str
    unverified_ids: tuple[str, ...]
    warnings: tuple[str, ...]


def _records(papers: ReferenceInput) -> ReferenceInput:
    grouped: dict[str, tuple[Paper, set[Alias]]] = {}
    for paper, aliases in papers:
        existing = grouped.get(paper.id)
        if existing is not None:
            if existing[0] != paper:
                raise ValueError("paper_metadata_conflict")
            existing[1].update(aliases)
        else:
            grouped[paper.id] = (paper, set(aliases))
    return tuple(
        (paper, tuple(sorted(aliases, key=lambda alias: (alias.scheme, alias.value))))
        for _, (paper, aliases) in sorted(grouped.items())
    )


def _identifiers(paper_id: str, aliases: tuple[Alias, ...], warnings: list[str]) -> dict[str, str]:
    identifiers: dict[str, str] = {}
    for scheme, normalize in (("doi", normalize_doi), ("arxiv", normalize_arxiv_id)):
        values: set[str] = set()
        invalid = False
        for alias in aliases:
            if alias.scheme != scheme:
                continue
            value = normalize(alias.value)
            if value is None or any(ord(char) < 32 or ord(char) == 127 for char in value):
                invalid = True
            else:
                values.add(value)
        if invalid:
            warnings.append(f"{paper_id}: 无效的 {scheme} 标识符未用于引用")
        if len(values) > 1:
            warnings.append(f"{paper_id}: 多个 {scheme} 标识符有歧义，未用于引用")
        elif values:
            identifiers[scheme] = next(iter(values))
    return identifiers


def _published(paper: Paper, warnings: list[str]) -> date | None:
    if paper.published_at is None:
        return None
    try:
        return date.fromisoformat(paper.published_at)
    except ValueError:
        warnings.append(f"{paper.id}: 发表日期不完整或无效，引用日期保持未知")
        return None


def _url(identifiers: dict[str, str]) -> str:
    if "arxiv" in identifiers:
        return "https://arxiv.org/abs/" + quote(identifiers["arxiv"], safe="/")
    return "https://doi.org/" + quote(identifiers["doi"], safe="/")


def _reference_type(paper: Paper, warnings: list[str]) -> tuple[str, str]:
    """只映射来源明确给出的文献形态；review 等内容标签不推断出版容器。"""
    keys = {
        "".join(char for char in unicodedata.normalize("NFKC", value).casefold() if char.isalnum())
        for value in paper.publication_types
    }
    types = {_REFERENCE_TYPES[key] for key in keys if key in _REFERENCE_TYPES}
    if len(types) > 1:
        warnings.append(f"{paper.id}: 来源文献类型互相冲突，引用使用通用类型")
        return _GENERIC_TYPE
    return next(iter(types)) if types else _GENERIC_TYPE


def _csl(
    paper: Paper, identifiers: dict[str, str], published: date | None, kind: str
) -> dict[str, object]:
    record: dict[str, object] = {"id": paper.id, "type": kind, "title": paper.title}
    if paper.authors:
        record["author"] = [{"literal": author} for author in paper.authors]
    if paper.venue:
        record["container-title"] = paper.venue
    if paper.issn:
        record["ISSN"] = paper.issn
    if published is not None:
        record["issued"] = {"date-parts": [[published.year, published.month, published.day]]}
    if "doi" in identifiers:
        record["DOI"] = identifiers["doi"]
    record["URL"] = _url(identifiers)
    return record


def _tex(value: str) -> str:
    """用户数据不能引入结构花括号、换行或可执行的 TeX 命令。"""
    printable = "".join(" " if ord(char) < 32 or ord(char) == 127 else char for char in value)
    return "".join(_TEX.get(char, char) for char in " ".join(printable.split()))


def _bibtex(paper: Paper, identifiers: dict[str, str], published: date | None, kind: str) -> str:
    fields = [("title", _tex(paper.title))]
    if paper.authors:
        authors = " and ".join("{" + _tex(author) + "}" for author in paper.authors)
        fields.append(("author", authors))
    if paper.venue:
        field = {"article": "journal", "incollection": "booktitle", "inproceedings": "booktitle"}
        fields.append((field.get(kind, "howpublished"), _tex(paper.venue)))
    if paper.issn:
        fields.append(("issn", _tex(paper.issn)))
    if published is not None:
        fields.extend(
            (
                ("year", str(published.year)),
                ("month", str(published.month)),
                ("date", published.isoformat()),
            )
        )
    if "doi" in identifiers:
        fields.append(("doi", _tex(identifiers["doi"])))
    if "arxiv" in identifiers:
        fields.extend((("eprint", _tex(identifiers["arxiv"])), ("archivePrefix", "arXiv")))
    fields.append(("url", _tex(_url(identifiers))))
    key = "litgleaner-" + hashlib.sha256(paper.id.encode()).hexdigest()
    return (
        f"@{kind}{{{key},\n"
        + "".join(f"  {name} = {{{value}}},\n" for name, value in fields)
        + "}\n"
    )


def render_references(papers: ReferenceInput) -> ReferenceExport:
    warnings: list[str] = []
    unverified: list[str] = []
    bibtex: list[str] = []
    csl: list[dict[str, object]] = []
    for paper, aliases in _records(papers):
        identifiers = _identifiers(paper.id, aliases, warnings)
        if not identifiers:
            unverified.append(paper.id)
            continue
        published = _published(paper, warnings)
        csl_kind, bib_kind = _reference_type(paper, warnings)
        bibtex.append(_bibtex(paper, identifiers, published, bib_kind))
        csl.append(_csl(paper, identifiers, published, csl_kind))
    return ReferenceExport(
        "\n".join(bibtex),
        json.dumps(csl, ensure_ascii=False, indent=2) + "\n",
        tuple(unverified),
        tuple(warnings),
    )
