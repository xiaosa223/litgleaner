"""命令行共用的参数、错误显示、加载和运行锁。"""

import fcntl
import os
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from functools import wraps
from pathlib import Path
from typing import Annotated

import typer

from litgleaner.application.context import AppContext
from litgleaner.bootstrap import CONTACT_EMAIL, build_context
from litgleaner.domain.errors import ConfigError
from litgleaner.infra.config import LoadedConfig, load_config
from litgleaner.infra.db.migrate import is_up_to_date

ConfigOption = Annotated[
    Path, typer.Option("--config", envvar="LITGLEANER_CONFIG", help="配置目录")
]
DEFAULT_CONFIG = Path("config")


def friendly[**P, R](command: Callable[P, R]) -> Callable[P, R]:
    """用户输入或配置错误只显示说明，不显示堆栈。真正的程序错误仍然会暴露。"""

    @wraps(command)
    def wrapper(*args: P.args, **kwargs: P.kwargs) -> R:
        try:
            return command(*args, **kwargs)
        except ConfigError as error:
            typer.secho(str(error), fg=typer.colors.RED, err=True)
            raise typer.Exit(2) from error

    return wrapper


def echo(message: str) -> None:
    typer.echo(message)


def load_or_exit(path: Path) -> LoadedConfig:
    try:
        return load_config(path)
    except ConfigError as error:
        typer.secho(str(error), fg=typer.colors.RED, err=True)
        raise typer.Exit(2) from error


def context_or_exit(path: Path) -> AppContext:
    config = load_or_exit(path)
    if not is_up_to_date(config.database_url):
        typer.secho("数据库不是最新版本，请先执行：litgleaner db upgrade", fg="red", err=True)
        raise typer.Exit(2)
    try:
        return build_context(config)
    except ConfigError as error:
        typer.secho(str(error), fg=typer.colors.RED, err=True)
        raise typer.Exit(2) from error


@contextmanager
def run_lock(config: LoadedConfig) -> Iterator[None]:
    """定时任务可能重叠。同一时间只允许一个流程运行，避免重复调用和重复计费。"""
    path = config.path(config.app.database).with_suffix(".lock")
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w") as handle:
        try:
            fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as error:
            typer.secho("已有流程正在运行，本次退出", fg="yellow", err=True)
            raise typer.Exit(3) from error
        yield


@contextmanager
def try_run_lock(config: LoadedConfig) -> Iterator[bool]:
    """和 run_lock 是同一把锁，但拿不到时不退出，返回 False 由调用方决定稍后再试。"""
    path = config.path(config.app.database).with_suffix(".lock")
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w") as handle:
        try:
            fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            yield False
            return
        yield True


# doctor 用的配置检查
_FAKE_PREFIX = "fake_"


def enabled_adapters(config: LoadedConfig) -> list[tuple[str, tuple[str, ...]]]:
    groups = [
        *config.sources.values(),
        *config.channels.values(),
        *config.llm.providers.values(),
        *config.app.fulltext,
        config.app.parser,
    ]
    return [(item.adapter, item.credentials_env) for item in groups if item.enabled]


def missing_optional(config: LoadedConfig) -> list[str]:
    groups = [*config.sources.values(), *config.app.fulltext]
    missing = sorted(
        {name for item in groups if item.enabled for name in item.optional_env}
        - {name for name in os.environ if os.environ[name]}
    )
    real = [i for i in groups if i.enabled and not i.adapter.startswith(_FAKE_PREFIX)]
    if real and not os.environ.get(CONTACT_EMAIL):
        missing.append(CONTACT_EMAIL)
    if not missing:
        return []
    return [f"可选环境变量未设置：{', '.join(missing)}。不影响运行，但部分来源额度更低或会跳过"]


def missing_credentials(config: LoadedConfig) -> list[str]:
    return [
        f"{adapter} 需要环境变量 {name}"
        for adapter, names in enabled_adapters(config)
        for name in names
        if not os.environ.get(name)
    ]


def fake_adapters(config: LoadedConfig) -> list[str]:
    fakes = sorted({name for name, _ in enabled_adapters(config) if name.startswith(_FAKE_PREFIX)})
    return [f"正在使用演示用的假适配器：{', '.join(fakes)}，结果不是真实数据"] if fakes else []
