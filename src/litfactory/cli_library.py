"""文献库和飞书相关的命令：授权 Zotero、手动加入论文、运行飞书按钮监听。"""

import logging
import queue
import threading
import time
from collections.abc import Callable
from pathlib import Path
from typing import Annotated

import typer

from litfactory.application.context import AppContext
from litfactory.application.library import find_paper, record_action, require_library
from litfactory.application.pipeline import run_pipeline
from litfactory.application.stages.publish import rerender
from litfactory.bootstrap import authorize_zotero, build_feishu_listener, zotero_status
from litfactory.cli_shared import (
    DEFAULT_CONFIG,
    ConfigOption,
    context_or_exit,
    echo,
    friendly,
    load_or_exit,
    run_lock,
    try_run_lock,
)
from litfactory.domain.errors import ConfigError, LitFactoryError
from litfactory.domain.library import CardAction
from litfactory.infra.config import LoadedConfig
from litfactory.ports.inbound import CardClick

logger = logging.getLogger(__name__)
LOCK_RETRY_SECONDS = 10
LOCK_RETRIES = 30

zotero_app = typer.Typer(help="Zotero 文献库", no_args_is_help=True)
feishu_app = typer.Typer(help="飞书渠道", no_args_is_help=True)


@zotero_app.command("authorize")
@friendly
def zotero_authorize(config: ConfigOption = DEFAULT_CONFIG) -> None:
    """在 Zotero 弹窗里授权论文工厂写入。请选“始终允许”。"""
    echo("请切到 Zotero，在弹出的窗口里点“始终允许”……")
    try:
        path = authorize_zotero(load_or_exit(config))
    except LitFactoryError as error:
        raise ConfigError(f"授权没有完成：{error}") from error
    echo(f"已授权。key 保存在 {path}（只有你能读）")


@zotero_app.command("status")
@friendly
def zotero_status_command(config: ConfigOption = DEFAULT_CONFIG) -> None:
    """检查 Zotero 是否在运行、是否已授权。只读，不写入。"""
    try:
        lines = zotero_status(load_or_exit(config))
    except LitFactoryError as error:
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
            echo("没写进去。按上面的提示处理后，运行 litfactory jobs retry")
        else:
            echo("暂时没写进去，已留在队列里，下次 litfactory run 时重试")
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
    listener, update = build_feishu_listener(loaded, channel, clicks.put)
    worker = threading.Thread(
        target=_work, args=(config, loaded, channel, clicks, update), daemon=True
    )
    worker.start()
    echo(f"开始监听飞书渠道 {channel} 的卡片按钮（Ctrl+C 退出）")
    listener.start()


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
            _handle_click(ctx, loaded, channel, click, update)
        except Exception:
            logger.exception("处理飞书点击 %s 失败", click.event_id)


def _handle_click(
    ctx: AppContext,
    loaded: LoadedConfig,
    channel: str,
    click: CardClick,
    update: Callable[[str, str], None],
) -> None:
    record_action(ctx, click.action, channel=channel, actor=click.actor, external_id=click.event_id)
    if click.action.action == "save_to_library" and ctx.library is not None:
        _save_now(ctx, loaded)
    if click.message_id and click.action.digest_id:
        rendered = rerender(ctx, click.action.digest_id, channel)
        if rendered is not None:
            update(click.message_id, rendered.body)


def _save_now(ctx: AppContext, loaded: LoadedConfig) -> None:
    """每日流程正在跑时等一会儿；一直拿不到锁就留给下一次运行。"""
    for _ in range(LOCK_RETRIES):
        with try_run_lock(loaded) as locked:
            if locked:
                report = run_pipeline(ctx, only="library")
                if not report.ok:
                    logger.warning("加入文献库没有完成：%s", report.summary())
                return
        time.sleep(LOCK_RETRY_SECONDS)
    logger.warning("流程一直在运行，选中的论文留在队列里，下次运行时加入")
