"""JSON 归档内容。没有时间戳，同样的数据永远生成同样的文件。"""

from collections.abc import Mapping
from typing import Any

from litfactory.domain.paper import Paper

ARCHIVE_SCHEMA_VERSION = 1


def build_archive_record(
    paper: Paper,
    aliases: Mapping[str, str],
    extraction: Mapping[str, Any] | None,
    assessments: Mapping[str, Mapping[str, Any]],
    fulltext_used: bool,
) -> dict[str, Any]:
    return {
        "schema_version": ARCHIVE_SCHEMA_VERSION,
        "id": paper.id,
        "aliases": dict(aliases),
        "title": paper.title,
        "abstract": paper.abstract,
        "authors": list(paper.authors),
        "venue": paper.venue,
        "published_at": paper.published_at,
        "fulltext_used": fulltext_used,
        "extraction": dict(extraction) if extraction else None,
        "assessments": {topic: dict(item) for topic, item in assessments.items()},
    }


def archive_key(paper: Paper) -> str:
    date = paper.published_at or paper.first_seen_at
    return f"papers/{date[:4]}/{date[5:7]}/{paper.id}.json"
