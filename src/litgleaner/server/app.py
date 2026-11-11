"""组装 FastAPI 应用：接口、安全检查、前端静态文件。"""

from pathlib import Path
from urllib.parse import unquote

from fastapi import FastAPI, Request, Response
from fastapi.concurrency import run_in_threadpool
from fastapi.responses import FileResponse, JSONResponse, PlainTextResponse, RedirectResponse

from litgleaner.application import inbox, reader, translation, translation_plan
from litgleaner.application.context import AppContext
from litgleaner.domain.errors import InvalidRequest, NotFound
from litgleaner.domain.inbox import InboxItem
from litgleaner.domain.reading import Annotation, NewAnnotation
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


def _paper_routes(app: FastAPI, ctx: AppContext) -> None:
    @app.get("/api/inbox")
    def inbox() -> reader.Inbox:
        return reader.inbox(ctx)

    @app.get("/api/costs")
    def costs(paper_id: str | None = None) -> dict[str, float | None]:
        paper_usd = ctx.store.runs.llm_cost_for_paper(paper_id) if paper_id else None
        return {"month_usd": reader.month_cost_usd(ctx), "paper_usd": paper_usd}

    @app.get("/api/papers/{paper_id}/translations")
    def translations(paper_id: str) -> list[translation.BlockTranslation]:
        return sorted(
            translation.cached_translations(ctx, paper_id).values(), key=lambda t: t.index
        )

    @app.post("/api/papers/{paper_id}/blocks/{index}/translate")
    def translate(paper_id: str, index: int) -> Response:
        return sse_response(translation.translate_block(ctx, paper_id, index))

    @app.post("/api/papers/{paper_id}/translate")
    def translate_many(paper_id: str, body: TranslateBody) -> Response:
        return sse_response(translation.translate_blocks(ctx, paper_id, body.indices))

    @app.get("/api/papers/{paper_id}/translation-plan")
    def translation_estimate(paper_id: str) -> translation_plan.TranslationPlan:
        return translation_plan.plan(ctx, paper_id)  # 只估算，不调用模型

    @app.post("/api/papers/{paper_id}/translation-plan", status_code=202)
    def translation_confirm(paper_id: str) -> translation_plan.TranslationPlan:
        return translation_plan.queue(ctx, paper_id)  # 用户确认后排队，由常驻服务执行

    @app.get("/api/papers/{paper_id}")
    def paper(paper_id: str) -> reader.PaperView:
        return reader.paper_view(ctx, paper_id)

    @app.get("/api/papers/{paper_id}/document")
    def document(paper_id: str) -> reader.DocumentView:
        view = reader.document_view(ctx, paper_id)
        if view is None:
            raise NotFound(f"这篇论文没有解析结果：{paper_id}")
        return view

    @app.get("/api/papers/{paper_id}/pdf")
    def pdf(paper_id: str) -> Response:
        content = reader.pdf_content(ctx, paper_id)
        # PDF 来自第三方：页面里由 pdf.js 读取；直接在地址栏打开时放进沙箱，禁止脚本
        headers = {"Content-Disposition": "inline", "Content-Security-Policy": "sandbox"}
        return Response(content, media_type="application/pdf", headers=headers)


def _inbox_routes(app: FastAPI, ctx: AppContext) -> None:
    @app.post("/api/inbox/papers")
    def add_paper(body: AddPaperBody) -> InboxItem:
        return inbox.add_identifier(ctx, body.text)

    @app.post(UPLOAD_PATH)
    async def upload_paper(request: Request) -> InboxItem:
        if request.headers.get("content-type", "").split(";")[0].strip() != "application/pdf":
            raise InvalidRequest("请求体必须是 PDF 文件（Content-Type: application/pdf）")
        name = unquote(request.headers.get("x-file-name", ""))[:255] or "未命名论文.pdf"
        identifier = (request.headers.get("x-identifier") or "")[:300] or None
        content = await request.body()
        return await run_in_threadpool(inbox.add_pdf, ctx, name, content, identifier)


def _user_routes(app: FastAPI, ctx: AppContext) -> None:
    @app.put("/api/papers/{paper_id}/reading", status_code=204)
    def reading(paper_id: str, body: ReadingBody) -> None:
        reader.set_reading_state(ctx, paper_id, body.status, body.position)

    @app.post("/api/papers/{paper_id}/annotations", status_code=201)
    def add_annotation(paper_id: str, body: AnnotationBody) -> Annotation:
        item = NewAnnotation(
            paper_id, body.kind, body.page, body.bbox, body.quote, body.body, tuple(body.rects)
        )
        return reader.add_annotation(ctx, item)

    @app.patch("/api/annotations/{annotation_id}", status_code=204)
    def edit_annotation(annotation_id: str, body: AnnotationEdit) -> None:
        reader.edit_annotation(ctx, annotation_id, body.body)

    @app.delete("/api/annotations/{annotation_id}", status_code=204)
    def remove_annotation(annotation_id: str) -> None:
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


def create_app(ctx: AppContext, security: LocalSecurity, static_dir: Path | None) -> FastAPI:
    app = FastAPI(title="拾穗领读器", docs_url=None, redoc_url=None, openapi_url=None)
    install_guard(app, security)

    @app.exception_handler(NotFound)
    def not_found(_request: Request, error: NotFound) -> JSONResponse:
        return JSONResponse({"detail": str(error)}, status_code=404)

    @app.exception_handler(InvalidRequest)
    def invalid(_request: Request, error: InvalidRequest) -> JSONResponse:
        return JSONResponse({"detail": str(error)}, status_code=422)

    _session_routes(app, security)
    _paper_routes(app, ctx)
    _user_routes(app, ctx)
    _inbox_routes(app, ctx)
    _static_routes(app, static_dir)
    return app
