"""添加论文时认出标识符（第 12.8 节）：只做格式识别，元数据由信号源给出。"""

import pytest

from litgleaner.domain.inbox import Identified, find_identifier, identify


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("10.1145/2939672.2939778", Identified("doi", "10.1145/2939672.2939778")),
        ("https://doi.org/10.1145/2939672.2939778.", Identified("doi", "10.1145/2939672.2939778")),
        ("doi: 10.48550/arXiv.1706.03762", Identified("doi", "10.48550/arxiv.1706.03762")),
        ("10.1000/a(b)", Identified("doi", "10.1000/a(b)")),
        ("(doi:10.1000/a(b)).", Identified("doi", "10.1000/a(b)")),
        ("1706.03762", Identified("arxiv", "1706.03762")),
        ("arXiv:1706.03762v7", Identified("arxiv", "1706.03762")),
        ("https://arxiv.org/pdf/1706.03762v7", Identified("arxiv", "1706.03762")),
        ("https://example.org/paper", Identified("url", "https://example.org/paper")),
    ],
)
def test_identify(text: str, expected: Identified):
    assert identify(text) == expected


@pytest.mark.parametrize("text", ["", "   ", "attention is all you need", "javascript:alert(1)"])
def test_identify_rejects_other_text(text: str):
    assert identify(text) is None


def test_find_identifier_in_first_page_prefers_doi():
    page = "Proceedings ... DOI: 10.1109/TPWRS.2020.1234567\narXiv:2001.00001"
    assert find_identifier(page) == Identified("doi", "10.1109/tpwrs.2020.1234567")


def test_find_identifier_reads_arxiv_stamp():
    page = "Attention Is All You Need\n...\narXiv:1706.03762v7 [cs.CL] 2 Aug 2023"
    assert find_identifier(page) == Identified("arxiv", "1706.03762")


def test_find_identifier_ignores_reference_list_dois():
    # 参考文献里的 DOI 不是这篇论文的：只看首页正文前段
    page = "Title\nAbstract text\n" + "x" * 5000 + "\n[1] doi:10.1000/other"
    assert find_identifier(page) is None
