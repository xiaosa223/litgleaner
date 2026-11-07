"""真实网络联调：逐个调用配置模板里的信号源和全文来源，报告能否工作。

不写数据库、不影响游标，只打印结果。配置里 enabled: false 的条目也会被测试。
缺少必需环境变量的来源会跳过并说明。

用法：
    export LITGLEANER_CONTACT_EMAIL="you@example.org"
    uv run python scripts/probe_sources.py                       # 全部来源
    uv run python scripts/probe_sources.py --only arxiv pubmed   # 指定来源
    uv run python scripts/probe_sources.py --days 7 --terms "single-cell RNA"
"""

import argparse
import logging
import os
import sys
import time
from pathlib import Path

from litgleaner.bootstrap import FULLTEXT, SOURCES, BuildArgs
from litgleaner.domain.errors import LitGleanerError
from litgleaner.domain.identity import Alias
from litgleaner.domain.query import (
    AuthorWatch,
    CitationWatch,
    InboxDrain,
    JournalWatch,
    KeywordSearch,
    Query,
)
from litgleaner.infra.config import AdapterConfig, load_config
from litgleaner.ports.fulltext import FullTextRequest

ROOT = Path(__file__).parents[1]
# 一篇在 arXiv、Unpaywall、OpenAlex、Semantic Scholar 都有开放 PDF 的论文。
DEFAULT_DOI = "10.1016/j.patcog.2023.109437"


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--config", type=Path, default=ROOT / "config")
    parser.add_argument("--only", nargs="*", default=None, help="只测这些名字（信号源或全文来源）")
    parser.add_argument("--days", type=int, default=3, help="回看天数")
    parser.add_argument("--terms", nargs="*", default=["large language model"])
    parser.add_argument("--issn", default="1476-4687", help="期刊追踪用的 ISSN（默认 Nature）")
    parser.add_argument("--seed-doi", default="10.1038/nature14539", help="引文追踪用的种子")
    parser.add_argument("--author", default="Yoshua Bengio")
    parser.add_argument("--doi", default=DEFAULT_DOI, help="全文来源用的 DOI")
    return parser.parse_args()


def queries(args: argparse.Namespace) -> list[Query]:
    return [
        KeywordSearch(terms=tuple(args.terms)),
        JournalWatch(issns=(args.issn,)),
        CitationWatch(dois=(args.seed_doi,)),
        AuthorWatch(names=(args.author,)),
        InboxDrain(),
    ]


def missing_env(config: AdapterConfig) -> list[str]:
    return [name for name in config.credentials_env if not os.environ.get(name)]


def build_args(name: str, config: AdapterConfig, root: Path, **extra: object) -> BuildArgs:
    credentials = {env: env for env in config.credentials_env}
    options = {**config.options, **extra}
    return BuildArgs(name, options, root, credentials, frozenset(config.optional_env))


def probe_source(name: str, config: AdapterConfig, args: argparse.Namespace, root: Path) -> None:
    source = SOURCES[config.adapter](build_args(name, config, root, lookback_days=args.days))
    for query in queries(args):
        if type(query) not in source.capabilities:
            continue
        started = time.monotonic()
        label = f"{name:<18}{type(query).__name__:<15}"
        try:
            result = source.fetch(query, None)
        except LitGleanerError as error:
            print(f"{label}失败  {type(error).__name__}: {str(error)[:120]}")
            continue
        sample = result.records[0].title[:60] if result.records else "-"
        notes = f"  告警：{'；'.join(result.warnings)[:80]}" if result.warnings else ""
        elapsed = time.monotonic() - started
        print(f"{label}{len(result.records):>4} 条  {elapsed:5.1f}s  {sample}{notes}")


def probe_fulltext(config: AdapterConfig, args: argparse.Namespace, root: Path) -> None:
    provider = FULLTEXT[config.adapter](build_args(config.adapter, config, root))
    request = FullTextRequest("probe", "probe", (Alias("doi", args.doi.lower()),))
    started = time.monotonic()
    label = f"{config.adapter:<18}{'全文':<15}"
    try:
        found = provider.fetch(request)
    except LitGleanerError as error:
        print(f"{label}失败  {type(error).__name__}: {str(error)[:120]}")
        return
    elapsed = time.monotonic() - started
    if found is None:
        print(f"{label}没有  {elapsed:5.1f}s")
    else:
        print(f"{label}{len(found.content):>9,} 字节  {elapsed:5.1f}s  {found.license_basis}")


def main() -> int:
    args = parse_args()
    logging.basicConfig(level=logging.WARNING, format="  %(name)s: %(message)s")
    config = load_config(args.config)
    wanted = set(args.only) if args.only else None
    for name, entry in config.sources.items():
        if entry.adapter.startswith("fake_") or (wanted and name not in wanted):
            continue
        if missing := missing_env(entry):
            print(f"{name:<18}跳过：缺少环境变量 {', '.join(missing)}")
            continue
        probe_source(name, entry, args, config.root)
    for entry in config.app.fulltext:
        if entry.adapter.startswith("fake_") or (wanted and entry.adapter not in wanted):
            continue
        if missing := missing_env(entry):
            print(f"{entry.adapter:<18}跳过：缺少环境变量 {', '.join(missing)}")
            continue
        probe_fulltext(entry, args, config.root)
    return 0


if __name__ == "__main__":
    sys.exit(main())
