"""组装 FastAPI 应用：接口、安全检查、前端静态文件。"""

from collections.abc import Callable
from pathlib import Path
from urllib.parse import unquote

from fastapi import FastAPI, Query, Request, Response
from fastapi.concurrency import run_in_threadpool
from fastapi.exceptions import RequestValidationError
from fastapi.responses import FileResponse, JSONResponse, PlainTextResponse, RedirectResponse

from litgleaner.application import catalog, inbox, reader, search, translation, translation_plan
from litgleaner.application.context import AppContext
from litgleaner.domain.errors import (
    BudgetExceeded,
    ConfigError,
    InvalidRequest,
    ModelConfigurationRequired,
    NotFound,
)
from litgleaner.domain.inbox import InboxItem
from litgleaner.domain.reading import Annotation, NewAnnotation
from litgleaner.ports.catalog import CatalogQuery, StatusFilter
from litgleaner.ports.model_settings import ModelSettings
from litgleaner.server.knowledge import install_knowledge_routes
from litgleaner.server.model_setup import install_model_routes
from litgleaner.server.qa import install_qa_routes
from litgleaner.server.runtime import Context
from litgleaner.server.schemas import (
    AddPaperBody,
    AnnotationBody,
    AnnotationEdit,
    ReadingBody,
    TranslateBody,
)
from litgleaner.server.security import (
    LAUNCH_PATH,
    SESSION_COOKIE,
    UPLOAD_PATH,
    LocalSecurity,
    install_guard,
)
from litgleaner.server.streaming import sse_response

NOT_BUILT = "前端还没有构建：在 web/ 目录执行 pnpm install && pnpm build。接口照常可用。"


def _session_routes(app: FastAPI, security: LocalSecurity) -> None:
    @app.get(LAUNCH_PATH, include_in_schema=False)
    def launch(token: str | None = None) -> Response:
        if not security.launch_ok(token):
            return PlainTextResponse("启动链接无效或已过期，请重新运行 litgleaner serve", 403)
        response = RedirectResponse("/", status_code=303)
        response.set_cookie(
            SESSION_COOKIE, security.new_session(), httponly=True, samesite="strict", path="/"
        )
        return response

    @app.get("/api/session")
    def session(request: Request) -> dict[str, str | None]:
        return {"csrf_token": security.csrf_token(request.cookies.get(SESSION_COOKIE))}


def _paper_routes(app: FastAPI) -> None:
    @app.get("/api/inbox")
    def inbox(ctx: Context) -> reader.Inbox:
        return reader.inbox(ctx)

    @app.get("/api/costs")
    def costs(ctx: Context, paper_id: str | None = None) -> dict[str, float | None]:
        paper_usd = ctx.store.runs.llm_cost_for_paper(paper_id) if paper_id else None
        return {"month_usd": reader.month_cost_usd(ctx), "paper_usd": paper_usd}

    @app.get("/api/papers/{paper_id}/translations")
    def translations(ctx: Context, paper_id: str) -> list[translation.BlockTranslation]:
        return sorted(
            translation.cached_translations(ctx, paper_id).values(), key=lambda t: t.index
        )

    @app.post("/api/papers/{paper_id}/blocks/{index}/translate")
    def translate(ctx: Context, paper_id: str, index: int) -> Response:
        return sse_response(translation.translate_block(ctx, paper_id, index))

    @app.post("/api/papers/{paper_id}/translate")
    def translate_many(ctx: Context, paper_id: str, body: TranslateBody) -> Response:
        return sse_response(translation.translate_blocks(ctx, paper_id, body.indices))

    @app.get("/api/papers/{paper_id}/translation-plan")
    def translation_estimate(ctx: Context, paper_id: str) -> translation_plan.TranslationPlan:
        return translation_plan.plan(ctx, paper_id)  # 只估算，不调用模型

    @app.post("/api/papers/{paper_id}/translation-plan", status_code=202)
    def translation_confirm(ctx: Context, paper_id: str) -> translation_plan.TranslationPlan:
        return translation_plan.queue(ctx, paper_id)  # 用户确认后排队，由常驻服务执行

    @app.get("/api/papers/{paper_id}")
    def paper(ctx: Context, paper_id: str) -> reader.PaperView:
        return reader.paper_view(ctx, paper_id)

    @app.get("/api/papers/{paper_id}/document")
    def document(ctx: Context, paper_id: str) -> reader.DocumentView:
        view = reader.document_view(ctx, paper_id)
        if view is None:
            raise NotFound(f"这篇论文没有解析结果：{paper_id}")
        return view

    @app.get("/api/papers/{paper_id}/pdf")
    def pdf(ctx: Context, paper_id: str) -> Response:
        content = reader.pdf_content(ctx, paper_id)
        # PDF 来自第三方：页面里由 pdf.js 读取；直接在地址栏打开时放进沙箱，禁止脚本
        headers = {"Content-Disposition": "inline", "Content-Security-Policy": "sandbox"}
        return Response(content, media_type="application/pdf", headers=headers)


def _inbox_routes(app: FastAPI) -> None:
    @app.get("/api/search")
    def search_all(
        ctx: Context,
        q: str = Query(default="", max_length=500),
        limit: int = Query(default=30, ge=1, le=100),
    ) -> search.SearchResults:
        return search.search(ctx, q, limit)

    @app.get("/api/library")
    def library(
        ctx: Context,
        *,
        status: StatusFilter = "all",
        topic_id: str | None = Query(default=None, max_length=200),
        q: str = Query(default="", max_length=500),
        limit: int = Query(default=50, ge=1, le=200),
        offset: int = Query(default=0, ge=0),
    ) -> catalog.LibraryPage:
        return catalog.library(ctx, CatalogQuery(status, topic_id, q, limit, offset))

    @app.post("/api/inbox/papers")
    def add_paper(ctx: Context, body: AddPaperBody) -> InboxItem:
        return inbox.add_identifier(ctx, body.text)

    @app.post(UPLOAD_PATH)
    async def upload_paper(ctx: Context, request: Request) -> InboxItem:
        if request.headers.get("content-type", "").split(";")[0].strip() != "application/pdf":
            raise InvalidRequest("请求体必须是 PDF 文件（Content-Type: application/pdf）")
        name = unquote(request.headers.get("x-file-name", ""))[:255] or "未命名论文.pdf"
        identifier = (request.headers.get("x-identifier") or "")[:300] or None
        content = await request.body()
        return await run_in_threadpool(inbox.add_pdf, ctx, name, content, identifier)


def _user_routes(app: FastAPI) -> None:
    @app.put("/api/papers/{paper_id}/reading", status_code=204)
    def reading(ctx: Context, paper_id: str, body: ReadingBody) -> None:
        reader.set_reading_state(ctx, paper_id, body.status, body.position)

    @app.post("/api/papers/{paper_id}/annotations", status_code=201)
    def add_annotation(ctx: Context, paper_id: str, body: AnnotationBody) -> Annotation:
        item = NewAnnotation(
            paper_id, body.kind, body.page, body.bbox, body.quote, body.body, tuple(body.rects)
        )
        return reader.add_annotation(ctx, item)

    @app.patch("/api/annotations/{annotation_id}", status_code=204)
    def edit_annotation(ctx: Context, annotation_id: str, body: AnnotationEdit) -> None:
        reader.edit_annotation(ctx, annotation_id, body.body)

    @app.delete("/api/annotations/{annotation_id}", status_code=204)
    def remove_annotation(ctx: Context, annotation_id: str) -> None:
        reader.remove_annotation(ctx, annotation_id)


def _static_routes(app: FastAPI, static_dir: Path | None) -> None:
    root = static_dir.resolve() if static_dir and (static_dir / "index.html").is_file() else None

    @app.get("/{path:path}", include_in_schema=False)
    def frontend(path: str) -> Response:
        if path.startswith("api/"):
            return JSONResponse({"detail": "接口不存在"}, status_code=404)
        if root is None:
            return PlainTextResponse(NOT_BUILT)
        index = root / "index.html"
        target = (root / path).resolve()
        if path and target.is_relative_to(root) and target.is_file() and target != index:
            # Windows 上 mimetypes 读注册表，可能认不出 .wasm；类型不对浏览器就不能编译 PDF 引擎
            media_type = "application/wasm" if target.suffix == ".wasm" else None
            return FileResponse(target, media_type=media_type)
        # 前端路由由页面处理。index.html 每次都向服务确认：重新构建或升级后，
        # 旧页面引用的带哈希的文件已经不在，缓存它会打不开（ARCHITECTURE 15.5）
        return FileResponse(index, headers={"Cache-Control": "no-cache"})


def _configuration_errors(app: FastAPI) -> None:
    @app.exception_handler(ModelConfigurationRequired)
    def missing_model(_request: Request, error: ModelConfigurationRequired) -> JSONResponse:
        return JSONResponse(
            {"code": error.code, "detail": str(error), "task": error.task}, status_code=409
        )

    @app.exception_handler(BudgetExceeded)
    def budget(_request: Request, error: BudgetExceeded) -> JSONResponse:
        return JSONResponse({"code": "budget_exceeded", "detail": str(error)}, status_code=402)

    @app.exception_handler(ConfigError)
    def configuration(_request: Request, _error: ConfigError) -> JSONResponse:
        return JSONResponse(
            {"code": "configuration_error", "detail": "模型设置失败，请检查配置和系统钥匙串"},
            status_code=422,
        )

    @app.exception_handler(RequestValidationError)
    def validation(_request: Request, _error: RequestValidationError) -> JSONResponse:
        return JSONResponse(
            {"code": "invalid_request", "detail": "请求参数不符合要求"}, status_code=422
        )


def create_app(
    ctx: AppContext,
    security: LocalSecurity,
    static_dir: Path | None,
    *,
    context_provider: Callable[[], AppContext] | None = None,
    model_settings: ModelSettings | None = None,
) -> FastAPI:
    app = FastAPI(title="拾穗领读器", docs_url=None, redoc_url=None, openapi_url=None)
    install_guard(app, security)
    app.state.context_provider = context_provider or (lambda: ctx)
    app.state.model_settings = model_settings
    _configuration_errors(app)

    @app.exception_handler(NotFound)
    def not_found(_request: Request, error: NotFound) -> JSONResponse:
        return JSONResponse({"detail": str(error)}, status_code=404)

    @app.exception_handler(InvalidRequest)
    def invalid(_request: Request, error: InvalidRequest) -> JSONResponse:
        return JSONResponse({"code": "invalid_request", "detail": str(error)}, status_code=422)

    _session_routes(app, security)
    _paper_routes(app)
    _user_routes(app)
    _inbox_routes(app)
    install_model_routes(app)
    install_qa_routes(app)
    install_knowledge_routes(app)
    _static_routes(app, static_dir)
    return app
