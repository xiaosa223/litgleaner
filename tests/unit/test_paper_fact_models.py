"""来源事实严格验证，不从标题猜测文献类型。"""

from dataclasses import replace

import pytest
from pydantic import ValidationError

from litgleaner.domain.paper import Paper, SourceRecord


def test_defaults_remain_compatible_with_old_paper_and_source_constructors():
    paper = Paper("P", "A Survey", "asurvey", None, (), None, None, None, "t")
    source = SourceRecord(title="A Review with 100 citations")
    assert paper.publication_types == source.publication_types == ()
    assert paper.citation_count is source.citation_count is None


def test_source_facts_roundtrip_json_with_original_type_names():
    source = SourceRecord(
        title="Original title", publication_types=("Review", "系统综述"), citation_count=0
    )
    assert SourceRecord.model_validate_json(source.model_dump_json()) == source
    assert source.publication_types == ("Review", "系统综述")
    assert source.citation_count == 0


@pytest.mark.parametrize("count", [True, False, -1, 1.5, 1.0, "10"])
def test_citation_count_requires_nonnegative_integer_without_coercion(count):
    with pytest.raises(ValidationError):
        SourceRecord(title="T", citation_count=count)
    paper = Paper("P", "T", "t", None, (), None, None, None, "t")
    with pytest.raises(ValueError, match="citation_count"):
        replace(paper, citation_count=count)


@pytest.mark.parametrize("types", [("",), (" \t\n",), ("Review", ""), (1,), (None,)])
def test_publication_types_require_nonblank_strings(types):
    with pytest.raises(ValidationError):
        SourceRecord(title="T", publication_types=types)
    paper = Paper("P", "T", "t", None, (), None, None, None, "t")
    with pytest.raises(ValueError, match="publication_types"):
        replace(paper, publication_types=types)
