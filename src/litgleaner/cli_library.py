"""文献库、个人数据导出及飞书相关的命令。"""

import json
import logging
import queue
import threading
import time
from collections.abc import Callable
from dataclasses import asdict
from pathlib import Path
from typing import Annotated, cast, get_args

import typer

from litgleaner.adapters.feishu.listener import answerers_from
from litgleaner.application import embeddings, outline
from litgleaner.application.context import AppContext
from litgleaner.application.export import mine
from litgleaner.application.inbound import apply_click
from litgleaner.application.library import find_paper, record_action, require_library
from litgleaner.application.pipeline import run_pipeline
from litgleaner.application.stale import report as stale_report
from litgleaner.application.topic_export import (
    ReferenceFormat,
    TableFormat,
    comparison,
    reference_text,
    references,
    table_text,
)
from litgleaner.bootstrap import authorize_zotero, build_feishu_listener, zotero_status
from litgleaner.cli_shared import (
    DEFAULT_CONFIG,
    ConfigOption,
    context_or_exit,
    echo,
    friendly,
    load_or_exit,
    run_lock,
    try_run_lock,
)
from litgleaner.domain.errors import ConfigError, InvalidRequest, LitGleanerError, NotFound
from litgleaner.domain.library import CardAction
from litgleaner.domain.outline_render import render_outline
from litgleaner.domain.stale import StaleKind
from litgleaner.infra.config import LoadedConfig
from litgleaner.infra.export_files import write_directory
from litgleaner.ports.inbound import CardClick

logger = logging.getLogger(__name__)
LOCK_RETRY_SECONDS = 10

zotero_app = typer.Typer(help="Zotero 文献库", no_args_is_help=True)
feishu_app = typer.Typer(help="飞书渠道", no_args_is_help=True)
export_app = typer.Typer(help="导出课题对比表、引用、综述骨架与个人数据", no_args_is_help=True)
memory_app = typer.Typer(help="论文间记忆与本地向量", no_args_is_help=True)


@memory_app.command("reembed")
@friendly
def memory_reembed(
    execute: Annotated[bool, typer.Option(help="显式计算本地向量，首次使用可能下载模型")] = False,
    config: ConfigOption = DEFAULT_CONFIG,
) -> None:
    """默认预览完整文库的待算数量；加 --execute 才加载本地模型并写入。"""
    ctx = context_or_exit(config)
    if execute:
        with run_lock(load_or_exit(config)):
            result = asdict(embeddings.reembed(ctx))
    else:
        result = asdict(embeddings.preview(ctx))
    echo(json.dumps(result, ensure_ascii=False))


@friendly
def stale_command(
    format: Annotated[str, typer.Option(help="输出格式：text 或 json")] = "text",
    kind: Annotated[str | None, typer.Option(help="限定结果类型，逗号分隔")] = None,
    include_current: Annotated[bool, typer.Option(help="文本中同时列出完整核验通过的记录")] = False,
    config: ConfigOption = DEFAULT_CONFIG,
) -> None:
    """零费用报告派生结果是否过期；保留历史，不重新排队或调用模型。"""
    if format not in ("text", "json"):
        raise typer.BadParameter("格式必须是 text 或 json", param_hint="--format")
    kinds = () if kind is None else tuple(part.strip() for part in kind.split(","))
    if any(part not in get_args(StaleKind) for part in kinds):
        raise typer.BadParameter(
            "结果类型无效：" + ", ".join(get_args(StaleKind)), param_hint="--kind"
        )
    report = stale_report(context_or_exit(config))
    items = tuple(entry for entry in report.items if not kinds or entry.kind in kinds)
    counts = {
        status: sum(entry.status == status for entry in items)
        for status in ("current", "stale", "unknown")
    }
    if format == "json":
        echo(
            json.dumps(
                {"total": len(items), **counts, "items": [asdict(entry) for entry in items]},
                ensure_ascii=False,
            )
        )
        return
    echo(
        f"核对 {len(items)} 项：有效 {counts['current']}，过期 {counts['stale']}，"
        f"未完整核验 {counts['unknown']}"
    )
    for entry in items:
        if include_current or entry.status != "current":
            echo(json.dumps(asdict(entry), ensure_ascii=False))


@export_app.command("mine")
@friendly
def export_mine(
    out: Annotated[Path, typer.Option(help="导出到新目录；已有目录不会覆盖")],
    include_pdfs: Annotated[bool, typer.Option(help="一并导出本地已有的原 PDF")] = False,
    config: ConfigOption = DEFAULT_CONFIG,
) -> None:
    """导出全部论文、笔记、问答、概念与引用，不调用模型。"""
    ctx = context_or_exit(config)
    try:
        count = write_directory(mine(ctx, include_pdfs=include_pdfs), out)
    except InvalidRequest as error:
        typer.secho(str(error), fg="red", err=True)
        raise typer.Exit(1) from error
    echo(f"已导出 {count} 个文件到 {out}")


@export_app.command("table")
@friendly
def export_table(
    topic: Annotated[str, typer.Option(help="当前研究课题编号")],
    format: Annotated[str, typer.Option(help="格式：md 或 csv")] = "md",
    tier: Annotated[str | None, typer.Option(help="按当前有效等级筛选，逗号分隔")] = None,
    config: ConfigOption = DEFAULT_CONFIG,
) -> None:
    """从已有提取和当前评分导出对比表；可重定向保存文件，不调用模型。"""
    if format not in ("md", "csv"):
        raise typer.BadParameter("格式必须是 md 或 csv", param_hint="--format")
    try:
        result = comparison(context_or_exit(config), topic, tier=tier)
        typer.echo(table_text(result, cast(TableFormat, format)), nl=False)
    except (InvalidRequest, NotFound) as error:
        typer.secho(str(error), fg="red", err=True)
        raise typer.Exit(2) from error


@export_app.command("refs")
@friendly
def export_refs(
    topic: Annotated[str, typer.Option(help="当前研究课题编号")],
    format: Annotated[str, typer.Option(help="格式：bibtex 或 csl-json")] = "bibtex",
    only_read: Annotated[bool, typer.Option(help="仅包含手动或阅读流程标为已读的论文")] = False,
    config: ConfigOption = DEFAULT_CONFIG,
) -> None:
    """从来源元数据导出引用；需要人工核对的论文另列在标准错误输出。"""
    if format not in ("bibtex", "csl-json"):
        raise typer.BadParameter("格式必须是 bibtex 或 csl-json", param_hint="--format")
    try:
        result = references(context_or_exit(config), topic, only_read=only_read)
        typer.echo(reference_text(result, cast(ReferenceFormat, format)), nl=False)
    except (InvalidRequest, NotFound) as error:
        typer.secho(str(error), fg="red", err=True)
        raise typer.Exit(2) from error
    for paper in result.needs_verification:
        typer.echo(
            "需要人工核对："
            + json.dumps({"paper_id": paper.id, "title": paper.title}, ensure_ascii=False),
            err=True,
        )
    for warning in result.references.warnings:
        typer.echo("警告：" + json.dumps(warning, ensure_ascii=False), err=True)


@export_app.command("outline")
@friendly
def export_outline(
    topic: Annotated[str, typer.Option(help="当前研究课题编号")],
    execute: Annotated[
        bool, typer.Option("--execute", help="确认预览并排队，会使用模型预算")
    ] = False,
    plan: Annotated[str | None, typer.Option(help="预览返回的 plan_hash")] = None,
    result: Annotated[str | None, typer.Option(help="导出指定历史结果编号的 Markdown")] = None,
    config: ConfigOption = DEFAULT_CONFIG,
) -> None:
    """默认只预览已读论文范围和费用；生成需 --execute --plan，下载需 --result。"""
    if (execute and plan is None) or (plan is not None and not execute):
        raise typer.BadParameter("生成必须同时提供 --execute 和 --plan")
    if result is not None and execute:
        raise typer.BadParameter("--result 不能与 --execute 一起使用")
    ctx = context_or_exit(config)
    try:
        if result is not None:
            typer.echo(render_outline(outline.result(ctx, topic, result)), nl=False)
        elif execute:
            with run_lock(load_or_exit(config)):
                status = outline.queue(ctx, topic, plan or "")
            echo(json.dumps(asdict(status), ensure_ascii=False, default=lambda v: v.model_dump()))
        else:
            echo(json.dumps(asdict(outline.preview(ctx, topic)), ensure_ascii=False))
    except LitGleanerError as error:
        typer.secho(str(error), fg="red", err=True)
        raise typer.Exit(2) from error


@zotero_app.command("authorize")
@friendly
def zotero_authorize(config: ConfigOption = DEFAULT_CONFIG) -> None:
    """在 Zotero 弹窗里授权拾穗写入。请选“始终允许”。"""
    echo("请切到 Zotero，在弹出的窗口里点“始终允许”……")
    try:
        path = authorize_zotero(load_or_exit(config))
    except LitGleanerError as error:
        raise ConfigError(f"授权没有完成：{error}") from error
    echo(f"已授权。key 保存在 {path}（只有你能读）")


@zotero_app.command("status")
@friendly
def zotero_status_command(config: ConfigOption = DEFAULT_CONFIG) -> None:
    """检查 Zotero 是否在运行、是否已授权。只读，不写入。"""
    try:
        lines = zotero_status(load_or_exit(config))
    except LitGleanerError as error:
        raise ConfigError(str(error)) from error
    for line in lines:
        echo(line)


@zotero_app.command("add")
@friendly
def zotero_add(
    reference: Annotated[str, typer.Argument(help="论文编号、DOI 或 arXiv 编号")],
    config: ConfigOption = DEFAULT_CONFIG,
) -> None:
    """把一篇论文加入 Zotero。没有飞书等渠道时用这个。"""
    loaded = load_or_exit(config)
    ctx = context_or_exit(config)
    library = require_library(ctx)
    paper_id = find_paper(ctx, reference)
    record_action(ctx, CardAction("save_to_library", paper_id), channel="cli")
    with run_lock(loaded):
        report = run_pipeline(ctx, only="library")
    entry = ctx.store.feedback.get_entry(paper_id, library.name)
    if entry is None:
        echo(report.summary())
        if any(stage.dead for stage in report.stages):
            echo("没写进去。按上面的提示处理后，运行 litgleaner jobs retry")
        else:
            echo("暂时没写进去，已留在队列里，下次 litgleaner run 时重试")
        raise typer.Exit(1)
    echo(
        ("已加入" if entry.created else "Zotero 里已经有这篇，已补上分组和标签")
        + f"：{entry.item_key}"
    )
    for warning in entry.warnings:
        typer.secho(f"警告：{warning}", fg="yellow")


@feishu_app.command("listen")
@friendly
def feishu_listen(
    channel: Annotated[str, typer.Option(help="channels.yaml 里的飞书渠道名")] = "me",
    config: ConfigOption = DEFAULT_CONFIG,
) -> None:
    """常驻运行：接收飞书卡片上的按钮点击，把选中的论文加入 Zotero。"""
    loaded = load_or_exit(config)
    context_or_exit(config)  # 先检查配置和数据库，有问题立刻报出来；工作线程另建连接
    clicks: queue.Queue[CardClick] = queue.Queue()
    try:
        listener, update = build_feishu_listener(loaded, channel, clicks.put)
    except ConfigError as error:
        if str(error) == "inbound_disabled":
            raise ConfigError(f"没有启用的飞书渠道：{channel}") from error
        raise
    worker = threading.Thread(
        target=_work, args=(config, loaded, channel, clicks, update), daemon=True
    )
    worker.start()
    echo(f"开始监听飞书渠道 {channel} 的卡片按钮（Ctrl+C 退出）")
    try:
        listener.start()
    finally:
        listener.stop()


def _work(
    path: Path,
    loaded: LoadedConfig,
    channel: str,
    clicks: "queue.Queue[CardClick]",
    update: Callable[[str, str], None],
) -> None:
    """数据库只在这个线程里使用。飞书回调线程只负责把点击放进队列。"""
    ctx = context_or_exit(path)
    while True:
        click = clicks.get()
        try:
            handled = _handle_click(ctx, loaded, channel, click, update)
        except Exception:
            logger.exception("处理飞书点击 %s 失败", click.event_id)
            handled = False
        if not handled:
            clicks.put(click)
            time.sleep(LOCK_RETRY_SECONDS)


def _handle_click(
    ctx: AppContext,
    loaded: LoadedConfig,
    channel: str,
    click: CardClick,
    update: Callable[[str, str], None],
) -> bool:
    """与服务共用核验和运行锁；锁被占用时由工作线程保留点击稍后再试。"""
    settings = loaded.channels.get(channel)
    if settings is None or not settings.enabled:
        return True
    allowed = {channel: answerers_from(settings.options)}
    with try_run_lock(loaded) as acquired:
        if not acquired:
            return False
        rendered = apply_click(ctx, click, allowed)
    if click.message_id and rendered is not None:
        try:
            update(click.message_id, rendered.body)
        except Exception:
            logger.exception("更新飞书卡片失败，已记录的反馈保留")
    return True
