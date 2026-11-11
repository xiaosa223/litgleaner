"""原始参考文献只提取显式标识符，保留 DOI 合法括号和未识别条目。"""

import pytest

from litgleaner.domain.citation import reference_aliases
from litgleaner.domain.identity import Alias


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("[1] Author. doi:10.1000/A(B).", (Alias("doi", "10.1000/a(b)"),)),
        ("See (https://doi.org/10.1000/abc).", (Alias("doi", "10.1000/abc"),)),
        ("arXiv:2401.12345v2, 2024.", (Alias("arxiv", "2401.12345"),)),
        ("https://arxiv.org/pdf/2401.12345.pdf", (Alias("arxiv", "2401.12345"),)),
        ("[4] Unknown author. A similar title.", ()),
        ("prefix110.1000/not-a-doi", ()),
        ("10.1000/ABC and 10.1000/abc", (Alias("doi", "10.1000/abc"),)),
    ],
)
def test_reference_identifier_extraction_is_conservative(text, expected):
    assert reference_aliases(text) == expected
