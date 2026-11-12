"""命令行入口。这里只做参数处理和输出，业务逻辑都在 application。"""

import json
import logging
from dataclasses import asdict
from datetime import UTC, datetime
from pathlib import Path
from typing import Annotated

import typer

from litgleaner import cli_library
from litgleaner.application.context import AppContext
from litgleaner.application.delivery_ops import resolve_delivery, unresolved_deliveries
from litgleaner.application.pipeline import ON_DEMAND_NAMES, STAGE_NAMES, run_pipeline
from litgleaner.application.rubric_ops import reassess, rescore
from litgleaner.application.stages.archive import write_archive
from litgleaner.application.survey import preview as preview_survey
from litgleaner.application.survey import queue as queue_survey
from litgleaner.application.topics import (
    WorkPlan,
    activate_topic,
    backfill_topic,
    pause_topic,
)
from litgleaner.bootstrap import build_context, zotero_status
from litgleaner.cli_library import export_app, feishu_app, memory_app, stale_command, zotero_app
from litgleaner.cli_shared import DEFAULT_CONFIG as _DEFAULT_CONFIG
from litgleaner.cli_shared import ConfigOption, fake_adapters, missing_credentials, missing_optional
from litgleaner.cli_shared import context_or_exit as _context
from litgleaner.cli_shared import echo as _echo
from litgleaner.cli_shared import friendly as _friendly
from litgleaner.cli_shared import load_or_exit as _config
from litgleaner.cli_shared import run_lock as _run_lock
from litgleaner.domain.errors import ConfigError, LitGleanerError
from litgleaner.infra.config import LoadedConfig
from litgleaner.infra.db.backup import check_snapshot, create_snapshot, upgrade_with_backup
from litgleaner.infra.db.migrate import is_up_to_date
from litgleaner.service import Service, open_in_browser

app = typer.Typer(help="拾穗：发现、评估并推送值得读的文献", no_args_is_help=True)
db_app = typer.Typer(help="数据库", no_args_is_help=True)
topic_app = typer.Typer(help="研究主题", no_args_is_help=True)
rubric_app = typer.Typer(help="评价指标", no_args_is_help=True)
jobs_app = typer.Typer(help="任务队列", no_args_is_help=True)
archive_app = typer.Typer(help="JSON 归档", no_args_is_help=True)
delivery_app = typer.Typer(help="推送记录", no_args_is_help=True)
for name, group in (
    ("db", db_app),
    ("topic", topic_app),
    ("rubric", rubric_app),
    ("jobs", jobs_app),
    ("archive", archive_app),
    ("delivery", delivery_app),
    ("zotero", zotero_app),
    ("feishu", feishu_app),
    ("export", export_app),
    ("memory", memory_app),
):
    app.add_typer(group, name=name)
app.command("stale")(stale_command)
app.command("reextract")(cli_library.reextract_command)

SinceOption = Annotated[
    datetime | None, typer.Option(formats=["%Y-%m-%d"], help="只处理此日期之后的论文")
]


def _print_plan(plan: WorkPlan, dry_run: bool, action: str) -> None:
    cost = "未知（还没有历史调用）" if plan.estimated_usd is None else f"${plan.estimated_usd:.4f}"
    _echo(f"{action}：{plan.papers} 篇，估算费用 {cost}")
    if dry_run:
        _echo("这是预览。确认后加 --execute 执行")
    else:
        _echo("已加入队列，下次 litgleaner run 时处理")


def _fail(error: LitGleanerError) -> typer.Exit:
    typer.secho(str(error), fg="red", err=True)
    return typer.Exit(1)


@db_app.command("upgrade")
def db_upgrade(config: ConfigOption = _DEFAULT_CONFIG) -> None:
    """创建或升级数据库。已有数据时先自动备份，备份失败就不迁移。"""
    loaded = _config(config)
    keep = loaded.app.backup.keep
    with _run_lock(loaded):
        try:
            snapshot = upgrade_with_backup(
                loaded.database_path, loaded.backup_dir, keep=keep, now=datetime.now(UTC)
            )
        except LitGleanerError as error:
            raise _fail(error) from error
    if snapshot is not None:
        _echo(f"升级前已备份到 {snapshot}")
    _echo("数据库已是最新版本")


@app.command()
def backup(config: ConfigOption = _DEFAULT_CONFIG) -> None:
    """生成数据库快照，只保留最新的若干份。流程运行中也可以执行。"""
    loaded = _config(config)
    try:
        result = create_snapshot(
            loaded.database_path,
            loaded.backup_dir,
            keep=loaded.app.backup.keep,
            now=datetime.now(UTC),
        )
    except LitGleanerError as error:
        raise _fail(error) from error
    _echo(f"已备份到 {result.path}")
    if result.removed:
        _echo(f"已清理 {len(result.removed)} 份旧快照")


@app.command()
def restore(
    snapshot: Annotated[Path, typer.Argument(exists=True, dir_okay=False, help="快照文件")],
    check: Annotated[bool, typer.Option("--check", help="在临时目录里演练还原并校验")] = False,
) -> None:
    """恢复演练：在临时目录还原快照，校验完整性、迁移版本和行数，不改动正在用的数据库。"""
    if not check:
        typer.secho("目前只支持 --check 演练；不会覆盖正在使用的数据库", fg="red", err=True)
        raise typer.Exit(2)
    report = check_snapshot(snapshot)
    _echo(f"迁移版本\t{report.revision or '未知'}")
    for table, count in report.row_counts.items():
        _echo(f"{table}\t{count}")
    if report.needs_upgrade and report.ok:
        _echo("快照来自旧版本，已在临时副本上升级到最新版本")
    for problem in report.problems:
        typer.secho(f"错误：{problem}", fg="red")
    if not report.ok:
        _echo("演练失败")
        raise typer.Exit(1)
    _echo("演练通过")


@app.command()
@_friendly
def run(
    config: ConfigOption = _DEFAULT_CONFIG,
    stage: Annotated[
        str | None, typer.Option(help=f"只运行一个阶段：{(*ON_DEMAND_NAMES, *STAGE_NAMES)}")
    ] = None,
) -> None:
    """运行流程。发生失败时退出码为 1，方便定时任务发现问题。"""
    loaded = _config(config)
    with _run_lock(loaded):
        report = run_pipeline(_context(config), only=stage)
    _echo(report.summary())
    if not report.ok:
        raise typer.Exit(1)


@app.command()
def serve(
    config: ConfigOption = _DEFAULT_CONFIG,
    port: Annotated[int, typer.Option(help="监听端口，0 表示随机", min=0, max=65535)] = 0,
    open_browser: Annotated[bool, typer.Option("--open/--no-open", help="启动后打开浏览器")] = True,
) -> None:
    """启动常驻服务：领读器（只监听本机）加定时运行流程。Ctrl-C 停止。"""
    loaded = _config(config)
    service = Service(loaded, _context(config), port=port)
    service.start()
    _echo(f"领读器已启动，用这个链接打开（每次启动都会变）：\n{service.launch_url}")
    if open_browser:
        open_in_browser(service.launch_url, loaded.database_path.parent)
    service.wait()


@app.command()
def doctor(config: ConfigOption = _DEFAULT_CONFIG) -> None:
    """检查配置、数据库、密钥和主题状态。"""
    loaded = _config(config)
    problems = missing_credentials(loaded)
    warnings = [*fake_adapters(loaded), *missing_optional(loaded)]
    if not is_up_to_date(loaded.database_url):
        problems.append("数据库不是最新版本，请执行 litgleaner db upgrade")
    else:
        try:
            ctx = build_context(loaded)
        except ConfigError as error:
            problems.append(str(error))
        else:
            warnings.extend(_runtime_warnings(ctx))
            warnings.extend(_library_warnings(loaded))
    for warning in warnings:
        typer.secho(f"警告：{warning}", fg="yellow")
    for problem in problems:
        typer.secho(f"错误：{problem}", fg="red")
    if problems:
        raise typer.Exit(1)
    _echo("检查通过")


def _runtime_warnings(ctx: AppContext) -> list[str]:
    warnings: list[str] = []
    states = ctx.store.judgments.topic_states()
    if not any(state.status == "active" for state in states):
        warnings.append("没有启用中的主题，请执行 litgleaner topic activate <id>")
    dead = ctx.store.jobs.list_by_status("dead")
    if dead:
        warnings.append(f"有 {len(dead)} 个死信任务，用 litgleaner jobs list 查看")
    return warnings


def _library_warnings(config: LoadedConfig) -> list[str]:
    library = config.app.library
    if library is None or not library.enabled or library.adapter != "zotero_local":
        return []
    try:
        lines = zotero_status(config)
    except LitGleanerError as error:
        return [f"Zotero 暂时写不进去（选中的论文会排队等待）：{error}"]
    return [line for line in lines if line.startswith("未授权")]


@topic_app.command("list")
def topic_list(config: ConfigOption = _DEFAULT_CONFIG) -> None:
    """列出所有主题和状态。"""
    ctx = _context(config)
    states = {state.topic_id: state.status for state in ctx.store.judgments.topic_states()}
    for topic in ctx.topics.values():
        _echo(f"{topic.id}\t{states.get(topic.id, 'paused')}\t{topic.name}")


@topic_app.command("activate")
@_friendly
def topic_activate(topic_id: str, config: ConfigOption = _DEFAULT_CONFIG) -> None:
    """启用主题。只处理之后发现的论文，历史论文用 backfill。"""
    activate_topic(_context(config), topic_id)
    _echo(f"已启用 {topic_id}")


@topic_app.command("pause")
@_friendly
def topic_pause(topic_id: str, config: ConfigOption = _DEFAULT_CONFIG) -> None:
    """暂停主题。不再消耗资源，数据全部保留。"""
    pause_topic(_context(config), topic_id)
    _echo(f"已暂停 {topic_id}")


@topic_app.command("backfill")
@_friendly
def topic_backfill(
    topic_id: str,
    since: SinceOption = None,
    execute: Annotated[bool, typer.Option(help="确认执行，默认只预览")] = False,
    config: ConfigOption = _DEFAULT_CONFIG,
) -> None:
    """用库内已有论文重新初筛。默认只预览数量和费用。"""
    start = since.date().isoformat() if since else None
    plan = backfill_topic(_context(config), topic_id, start, dry_run=not execute)
    _print_plan(plan, not execute, "待初筛")


@rubric_app.command("rescore")
def rubric_rescore(config: ConfigOption = _DEFAULT_CONFIG) -> None:
    """修改权重或等级线后重算，不产生 LLM 费用。"""
    loaded = _config(config)
    with _run_lock(loaded):
        report = rescore(_context(config))
    _echo(report.summary())
    if not report.ok:
        raise typer.Exit(1)


@rubric_app.command("reassess")
@_friendly
def rubric_reassess(
    topic_id: str,
    since: SinceOption = None,
    execute: Annotated[bool, typer.Option(help="确认执行，默认只预览")] = False,
    config: ConfigOption = _DEFAULT_CONFIG,
) -> None:
    """修改维度定义后重新评估，会产生 LLM 费用。默认只预览。"""
    start = since.date().isoformat() if since else None
    plan = reassess(_context(config), topic_id, start, dry_run=not execute)
    _print_plan(plan, not execute, "待重评")


@jobs_app.command("list")
def jobs_list(
    status: Annotated[str, typer.Option(help="pending 或 dead")] = "dead",
    config: ConfigOption = _DEFAULT_CONFIG,
) -> None:
    """查看任务。默认只看死信。"""
    if status not in {"pending", "dead"}:
        raise typer.BadParameter("只能是 pending 或 dead")
    jobs = _context(config).store.jobs.list_by_status("dead" if status == "dead" else "pending")
    for job in jobs:
        _echo(f"{job.stage}\t{job.attempts}\t{job.key}\t{job.last_error or ''}")
    _echo(f"共 {len(jobs)} 个")


@topic_app.command("survey")
@_friendly
def topic_survey(
    topic_id: str,
    execute: Annotated[bool, typer.Option("--execute", help="确认最近预览的计划")] = False,
    plan: Annotated[str | None, typer.Option("--plan", help="指定已保存的预览编号")] = None,
    config: ConfigOption = _DEFAULT_CONFIG,
) -> None:
    """预览课题调研；确认后由常驻服务或 run --stage survey 分轮执行。"""
    ctx = _context(config)
    if execute:
        try:
            with _run_lock(_config(config)):
                queued = queue_survey(ctx, topic_id, plan)
        except LitGleanerError as error:
            typer.secho(str(error), fg="red", err=True)
            raise typer.Exit(2) from error
        _echo(json.dumps(asdict(queued), ensure_ascii=False, default=lambda v: v.model_dump()))
        return
    if plan is not None:
        raise typer.BadParameter("--plan 需与 --execute 一起使用")
    try:
        result = preview_survey(ctx, topic_id)
    except LitGleanerError as error:
        typer.secho(str(error), fg="red", err=True)
        raise typer.Exit(2) from error
    _echo(result.model_dump_json(indent=2))
    _echo("估算按历史单次费用；后续轮次按候选限额估算。null 表示暂无历史费用。")
    _echo(f"确认执行：topic survey {topic_id} --execute --plan {result.id}")


@jobs_app.command("retry")
def jobs_retry(config: ConfigOption = _DEFAULT_CONFIG) -> None:
    """把死信任务重新放回队列。"""
    ctx = _context(config)
    _echo(f"已放回 {ctx.store.jobs.retry_dead(ctx.now_iso())} 个任务")


@archive_app.command("rebuild")
def archive_rebuild(config: ConfigOption = _DEFAULT_CONFIG) -> None:
    """从数据库重新生成全部 JSON 归档。"""
    loaded = _config(config)
    with _run_lock(loaded):
        ctx = _context(config)
        papers = ctx.store.judgments.papers_with_scores()
        for paper_id in papers:
            write_archive(ctx, paper_id)
    _echo(f"已处理 {len(papers)} 篇")


@delivery_app.command("list")
def delivery_list(config: ConfigOption = _DEFAULT_CONFIG) -> None:
    """列出发送失败或结果未知的推送。"""
    items = unresolved_deliveries(_context(config))
    for digest_id, channel in items:
        _echo(f"{digest_id}\t{channel}")
    _echo(f"共 {len(items)} 个")


@delivery_app.command("resolve")
@_friendly
def delivery_resolve(
    digest_id: str,
    channel: str,
    sent: Annotated[bool, typer.Option("--sent", help="已确认对方收到")] = False,
    resend: Annotated[bool, typer.Option("--resend", help="已确认没收到，下次运行重发")] = False,
    config: ConfigOption = _DEFAULT_CONFIG,
) -> None:
    """确认结果未知的推送。--sent 和 --resend 必须二选一。"""
    if sent == resend:
        raise typer.BadParameter("--sent 和 --resend 必须二选一")
    resolve_delivery(_context(config), digest_id, channel, resend=resend)
    _echo("已处理")


@app.command()
def stats(config: ConfigOption = _DEFAULT_CONFIG) -> None:
    """查看各类数据量和信号源贡献。"""
    ctx = _context(config)
    for key, value in sorted(ctx.store.stats().items()):
        _echo(f"{key}\t{value}")
    for source, count in sorted(ctx.store.papers.source_contribution().items()):
        _echo(f"source:{source}\t{count}")
    _echo(f"llm_calls\t{ctx.store.runs.llm_call_count()}")


def configure_logging() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")
    logging.getLogger("alembic").setLevel(logging.WARNING)
    # httpx 在 INFO 级别会记录完整 URL，其中包括签名上传链接，不能写进日志
    for noisy in ("httpx", "httpcore"):
        logging.getLogger(noisy).setLevel(logging.WARNING)


def main() -> None:
    configure_logging()
    app()


if __name__ == "__main__":
    main()
