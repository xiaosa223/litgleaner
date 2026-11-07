"""全文来源按顺序尝试：某个来源出错时继续下一个，全部失败才把错误交给调度重试。"""

import pytest

from litgleaner.application.stages.fulltext import first_available
from litgleaner.domain.errors import AuthFailed, RateLimited
from litgleaner.ports.fulltext import FullText, FullTextRequest

REQUEST = FullTextRequest("P", "Paper", ())
PDF = b"%PDF-1.7 body"


class Provider:
    def __init__(self, name: str, result: FullText | Exception | None) -> None:
        self.name = name
        self.result = result
        self.calls = 0

    def fetch(self, request: FullTextRequest) -> FullText | None:
        self.calls += 1
        if isinstance(self.result, Exception):
            raise self.result
        return self.result


def test_error_in_one_provider_does_not_block_the_next():
    broken = Provider("a", RateLimited("busy"))
    good = Provider("b", FullText(PDF, "open", "b"))
    assert first_available((broken, good), REQUEST) == good.result


def test_non_pdf_content_is_skipped():
    html = Provider("a", FullText(b"<html>", "open", "a"))
    good = Provider("b", FullText(PDF, "open", "b"))
    assert first_available((html, good), REQUEST) == good.result


def test_nothing_found_without_errors_means_unavailable():
    assert first_available((Provider("a", None),), REQUEST) is None


def test_all_failed_with_errors_raises_first_error_for_retry():
    with pytest.raises(RateLimited):
        first_available((Provider("a", RateLimited("busy")), Provider("b", None)), REQUEST)


def test_permanent_errors_are_also_surfaced_when_nothing_found():
    with pytest.raises(AuthFailed):
        first_available((Provider("a", AuthFailed("key")),), REQUEST)


def test_retryable_error_is_preferred_over_permanent_one():
    providers = (Provider("a", AuthFailed("key")), Provider("b", RateLimited("busy")))
    with pytest.raises(RateLimited):
        first_available(providers, REQUEST)
