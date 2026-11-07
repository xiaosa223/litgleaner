"""架构规则靠测试执行，不靠自觉。"""

import ast
import re
from pathlib import Path

import pytest

SRC = Path(__file__).parents[2] / "src" / "litgleaner"
MAX_FILE_LINES = 400
MAX_FUNCTION_LINES = 50
VENDOR = re.compile(
    r"mineru|telegram|crossref|ieee|arxiv|openalex|unpaywall|pubmed|europe_?pmc"
    r"|semantic.?scholar|openreview|zotero|core\.ac\.uk",
    re.IGNORECASE,
)
PY_FILES = sorted(p for p in SRC.rglob("*.py") if "migrations" not in p.parts)


def _ids(paths: list[Path]) -> list[str]:
    return [str(path.relative_to(SRC)) for path in paths]


@pytest.mark.parametrize("path", PY_FILES, ids=_ids(PY_FILES))
def test_file_size(path: Path):
    assert len(path.read_text(encoding="utf-8").splitlines()) <= MAX_FILE_LINES


@pytest.mark.parametrize("path", PY_FILES, ids=_ids(PY_FILES))
def test_function_size(path: Path):
    tree = ast.parse(path.read_text(encoding="utf-8"))
    long_functions = [
        node.name
        for node in ast.walk(tree)
        if isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef)
        and (node.end_lineno or 0) - node.lineno + 1 > MAX_FUNCTION_LINES
    ]
    assert long_functions == []


FLOW = sorted(p for layer in ("application", "ports") for p in (SRC / layer).rglob("*.py"))


@pytest.mark.parametrize("path", FLOW, ids=_ids(FLOW))
def test_flow_code_never_names_a_vendor(path: Path):
    assert VENDOR.findall(path.read_text(encoding="utf-8")) == []


def test_no_print_in_library_code():
    for path in PY_FILES:
        calls = [
            node
            for node in ast.walk(ast.parse(path.read_text(encoding="utf-8")))
            if isinstance(node, ast.Call)
            and isinstance(node.func, ast.Name)
            and node.func.id == "print"
        ]
        assert calls == [], path
