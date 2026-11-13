"""常驻服务（第 15.5 节）：组合领读器、定时调度和可选的渠道监听。

- 只监听 127.0.0.1，端口默认随机；每次启动生成新的启动令牌。
- 调度和命令行共用同一把运行锁：命令行正在跑流程时，本轮直接跳过，下一轮再试。
- 按配置时区每天备份一次；同日重启复用已完成快照，失败留到后续调度重试。
- 渠道回调只入队，由调度线程持共享锁执行反馈和文献库任务。
"""

import html
import logging
import os
import queue
import socket
import threading
import webbrowser
from collections.abc import Callable
from dataclasses import replace
from datetime import date
from pathlib import Path

import uvicorn

from litgleaner.adapters.feishu.listener import FeishuListener
from litgleaner.adapters.secrets import SystemSecretStore
from litgleaner.application.context import AppContext
from litgleaner.application.inbound import apply_click
from litgleaner.application.pipeline import RunReport, has_on_demand_work, run_pipeline
from litgleaner.application.topic_setup import define_topic
from litgleaner.application.topics import activate_topic
from litgleaner.bootstrap import LLMS, build_feishu_listener, build_llm, build_model_client
from litgleaner.cli_shared import try_run_lock
from litgleaner.domain.errors import ConfigError, NotFound
from litgleaner.domain.glossary import Glossary, GlossaryTerm
from litgleaner.domain.topic import Topic, TopicQuery
from litgleaner.infra.config import LoadedConfig
from litgleaner.infra.db.backup import ensure_daily_snapshot
from litgleaner.infra.model_settings import ModelConfigFile, SecretOverlay, prepare, settings_view
from litgleaner.infra.topic_files import TopicFiles
from litgleaner.ports.inbound import CardClick
from litgleaner.ports.llm import LLM
from litgleaner.ports.model_settings import (
    ModelDraft,
    ModelSettingsView,
    probe_output_tokens,
)
from litgleaner.ports.secrets import SecretStore
from litgleaner.ports.topic_settings import TopicFields
from litgleaner.server.app import create_app
from litgleaner.server.security import LocalSecurity

logger = logging.getLogger(__name__)

DEFAULT_STATIC_DIR = Path(__file__).parent / "server" / "static"
DEFAULT_INTERVAL_SECONDS = 300
ON_DEMAND_TICK_SECONDS = 3  # 用户的操作（整篇翻译、添加论文）不等下一轮定时运行
_JOIN_TIMEOUT_SECONDS = 10


class ModelRuntime:
    """每次请求或调度取一个不可变快照；正在输出的流使用原来的快照。

    在领读器里改模型设置或新建课题时写配置文件，再替换之后的快照。
    """

    def __init__(self, config: LoadedConfig, ctx: AppContext, secrets: SecretStore) -> None:
        self._lock = threading.RLock()
        self._file = ModelConfigFile(config, secrets)
        self._topics = TopicFiles(config.settings_dir / "topics")
        self._ctx = ctx
        self._protocols = tuple(name for name in LLMS if not name.startswith("fake_"))

    def snapshot(self) -> AppContext:
        with self._lock:
            return self._ctx

    def view(self) -> ModelSettingsView:
        with self._lock:
            return settings_view(
                self._file.config, self._file.secrets, self._protocols, set(self._ctx.llm.routes)
            )

    def client(self, draft: ModelDraft) -> LLM:
        with self._lock:
            probe = replace(draft, max_output_tokens=probe_output_tokens(draft))
            candidate, name = prepare(self._file.config, probe, self._protocols)
            return build_model_client(
                candidate, draft.provider, SecretOverlay(self._file.secrets, name, draft.api_key)
            )

    def save(self, draft: ModelDraft) -> ModelSettingsView:
        with self._lock:
            candidate, name = prepare(self._file.config, draft, self._protocols)
            secrets = SecretOverlay(self._file.secrets, name, draft.api_key)
            try:
                gateway = build_llm(
                    candidate,
                    self._ctx.store,
                    self._ctx.clock,
                    secrets,
                )
                view = settings_view(candidate, secrets, self._protocols, set(gateway.routes))
            except ConfigError:
                raise ConfigError("model_settings_invalid") from None
            self._file.commit(candidate, name, draft.api_key)
            self._ctx = replace(self._ctx, llm=gateway)
            return view

    def create(self, fields: TopicFields) -> Topic:
        with self._lock:
            topic = define_topic(fields, self._ctx.topics.keys(), self._ctx.rubrics)
            self._topics.create(topic)
            self._ctx = replace(self._ctx, topics={**self._ctx.topics, topic.id: topic})
            return topic

    def apply_query(
        self, topic_id: str, query: TopicQuery, glossary: tuple[GlossaryTerm, ...]
    ) -> Topic:
        with self._lock:
            before = self._ctx.topics.get(topic_id)
            if before is None:
                raise NotFound(f"未找到主题：{topic_id}")
            after = before.model_copy(update={"query": query})
            terms = Glossary(target_lang=self._ctx.settings.reading_language, terms=glossary)
            self._topics.update(before, after, terms)
            self._ctx = replace(
                self._ctx,
                topics={**self._ctx.topics, topic_id: after},
                glossaries={**self._ctx.glossaries, topic_id: terms},
            )
            if self._ctx.store.judgments.get_topic_state(topic_id) is None:
                activate_topic(
                    self._ctx, topic_id
                )  # 只启用新课题；暂停的课题保持暂停，不悄悄开始花钱
            return after


def run_due(config: LoadedConfig, ctx: AppContext) -> RunReport | None:
    """跑一轮流水线。拿不到运行锁时返回 None。到推送时间才会推送，所以每轮都跑是安全的。"""
    with try_run_lock(config) as acquired:
        if not acquired:
            logger.info("另一个流程正在运行，本轮调度跳过")
            return None
        report = run_pipeline(ctx)
    log = logger.info if report.ok else logger.warning
    log("定时运行完成：%s", report.summary().replace("\n", "；"))
    return report


def open_in_browser(url: str, directory: Path) -> Path:
    """通过只有本人可读的跳转页打开启动链接。

    直接把链接交给浏览器时，令牌会出现在 open 等子进程的命令行参数里，本机其他用户用 ps 就能看到。
    """
    directory.mkdir(parents=True, exist_ok=True)
    path = (directory / "reader-launch.html").resolve()
    escaped = html.escape(url, quote=True)
    page = (
        '<!doctype html><meta charset="utf-8">'
        f'<meta http-equiv="refresh" content="0;url={escaped}">'
        f'<a href="{escaped}">打开拾穗领读器</a>'
    )
    descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
        handle.write(page)
    path.chmod(0o600)  # 文件已存在时 os.open 不会改权限
    webbrowser.open(path.as_uri())
    return path


class Service:
    def __init__(
        self,
        config: LoadedConfig,
        ctx: AppContext,
        *,
        static_dir: Path | None = DEFAULT_STATIC_DIR,
        port: int = 0,
        interval_seconds: int = DEFAULT_INTERVAL_SECONDS,
        secrets: SecretStore | None = None,
    ) -> None:
        self._config = config
        self._ctx = ctx
        self._models = ModelRuntime(config, ctx, secrets or SystemSecretStore())
        self._static_dir = static_dir
        self._port = port
        self._interval = interval_seconds
        self._stop = threading.Event()
        self._threads: list[threading.Thread] = []
        self._server: uvicorn.Server | None = None
        self._security: LocalSecurity | None = None
        self._backup_day: date | None = None
        self._backup_path: Path | None = None
        self._seen_inbox = ctx.store.inbox.ready_revision()  # 完成添加后才唤醒处理
        self._clicks: queue.Queue[CardClick] = queue.Queue()
        self._listener: FeishuListener | None = None
        self._update_message: Callable[[str, str], None] | None = None

    @property
    def base_url(self) -> str:
        return f"http://127.0.0.1:{self._require_security().port}"

    @property
    def launch_url(self) -> str:
        return self._require_security().launch_url

    def _require_security(self) -> LocalSecurity:
        if self._security is None:
            raise RuntimeError("服务还没有启动")
        return self._security

    def start(self) -> None:
        sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        if os.name == "nt":  # Windows 上 SO_REUSEADDR 允许别的进程抢占同一端口
            sock.setsockopt(socket.SOL_SOCKET, socket.SO_EXCLUSIVEADDRUSE, 1)  # type: ignore[attr-defined]
        else:
            sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        sock.bind(("127.0.0.1", self._port))
        self._security = LocalSecurity(port=sock.getsockname()[1])
        app = create_app(
            self._ctx,
            self._security,
            self._static_dir,
            context_provider=self._models.snapshot,
            model_settings=self._models,
            topic_settings=self._models,
        )
        settings = uvicorn.Config(
            app,
            log_level="warning",
            lifespan="off",
            access_log=False,
            server_header=False,
            proxy_headers=False,
        )
        self._server = uvicorn.Server(settings)
        server = self._server
        self._spawn("litgleaner-http", lambda: server.run(sockets=[sock]))
        self._spawn("litgleaner-scheduler", self._schedule)
        self._wait_until_listening()
        self._spawn("litgleaner-channel", self._listen)

    def _listen(self) -> None:
        try:
            listener, update = build_feishu_listener(self._config, None, self._clicks.put_nowait)
            self._listener, self._update_message = listener, update
            if self._stop.is_set():
                listener.stop()
                return
            listener.start()
        except ConfigError as error:
            if str(error) != "inbound_disabled":
                logger.error("渠道监听配置无效：%s", error)
        except Exception:
            logger.exception("渠道监听失败，领读器和定时流程继续运行")

    def _drain_clicks(self) -> None:
        listener = self._listener
        if listener is None:
            return
        for _ in range(32):
            with try_run_lock(self._config) as acquired:
                if not acquired or self._stop.is_set():
                    return
                try:
                    click = self._clicks.get_nowait()
                except queue.Empty:
                    return
                try:
                    rendered = apply_click(self._models.snapshot(), click, listener.channels)
                except Exception:
                    self._clicks.put_nowait(click)
                    logger.exception("处理渠道点击 %s 失败，将稍后重试", click.event_id)
                    return
            if rendered is not None and click.message_id and self._update_message is not None:
                try:
                    self._update_message(click.message_id, rendered.body)
                except Exception:
                    logger.exception("更新渠道卡片失败，已记录的反馈保留")

    def _spawn(self, name: str, target: Callable[[], object]) -> None:
        thread = threading.Thread(target=target, name=name, daemon=True)
        thread.start()
        self._threads.append(thread)

    def _wait_until_listening(self) -> None:
        server = self._server
        for _ in range(200):
            if server is not None and server.started:
                return
            if self._stop.wait(0.05):
                return
        raise RuntimeError("领读器接口没有在 10 秒内启动")

    def _schedule(self) -> None:
        while not self._stop.is_set():
            self._backup_if_due()
            try:
                self._drain_clicks()
                run_due(self._config, self._models.snapshot())
            except Exception:  # 调度线程不能因为一次失败退出，详情写进日志
                logger.exception("定时运行出错")
            self._serve_on_demand(self._interval)

    def _backup_if_due(self) -> None:
        """每天首轮可用调度补一次快照，失败不阻断其他工作，也不提前记为完成。"""
        try:
            now = self._ctx.clock()
            day = now.astimezone(self._config.timezone).date()
            if (
                self._backup_day == day
                and self._backup_path is not None
                and self._backup_path.is_file()
            ):
                return
            with try_run_lock(self._config) as acquired:
                if not acquired:
                    return
                path = ensure_daily_snapshot(
                    self._config.database_path,
                    self._config.backup_dir,
                    keep=self._config.app.backup.keep,
                    now=now,
                    timezone=self._config.timezone,
                )
            self._backup_path, self._backup_day = path, day
            logger.info("每日备份已就绪：%s", path)
        except Exception:
            logger.exception("每日备份失败，将在后续调度重试")

    def _serve_on_demand(self, seconds: float) -> None:
        """两次定时运行之间，每隔几秒检查用户的操作：有按需任务或新添加的论文时立即跑一轮流程。

        流程里按需阶段排在最前；发现阶段按天幂等，提前跑不会重复抓取或重复收费。
        """
        ticks = max(1, int(seconds // ON_DEMAND_TICK_SECONDS))
        for _ in range(ticks):
            if self._stop.wait(min(seconds, ON_DEMAND_TICK_SECONDS)):
                return
            try:
                self._drain_clicks()
                ctx = self._models.snapshot()
                latest = ctx.store.inbox.ready_revision()
                wanted = has_on_demand_work(ctx) or latest != self._seen_inbox
                if wanted and run_due(self._config, ctx) is not None:
                    self._seen_inbox = latest
            except Exception:
                logger.exception("处理用户操作时出错")

    def stop(self) -> None:
        self._stop.set()
        if self._listener is not None:
            self._listener.stop()
        if self._server is not None:
            self._server.should_exit = True
        for thread in self._threads:
            thread.join(_JOIN_TIMEOUT_SECONDS)
        self._threads.clear()

    def wait(self) -> None:
        """阻塞到 Ctrl-C。"""
        try:
            while not self._stop.wait(1):
                pass
        except KeyboardInterrupt:
            logger.info("收到中断，正在停止")
        finally:
            self.stop()
