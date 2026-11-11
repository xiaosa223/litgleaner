"""个人数据以下载包返回，不接受网页指定本机写入路径。"""

from collections.abc import Iterator
from typing import BinaryIO

from fastapi import FastAPI, Query, Response
from fastapi.responses import StreamingResponse
from pydantic import Field
from starlette.types import Receive, Scope, Send

from litgleaner.application.export import mine
from litgleaner.application.topic_export import (
    ReferenceFormat,
    TableFormat,
    TopicComparison,
    TopicReferences,
    comparison,
    reference_text,
    references,
    table_text,
)
from litgleaner.domain.errors import InvalidRequest
from litgleaner.infra.export_files import zip_archive
from litgleaner.server.runtime import Context
from litgleaner.server.schemas import _Body


class MineBody(_Body):
    include_pdfs: bool = Field(default=False, strict=True)


_EMPTY = MineBody()


def _chunks(archive: BinaryIO) -> Iterator[bytes]:
    try:
        while chunk := archive.read(64 * 1024):
            yield chunk
    finally:
        archive.close()


class ExportResponse(StreamingResponse):
    """整个响应生命周期负责临时包，发送前失败和取消也确定性关闭。"""

    def __init__(self, archive: BinaryIO) -> None:
        self._archive = archive
        super().__init__(
            _chunks(archive),
            media_type="application/zip",
            headers={
                "Content-Disposition": 'attachment; filename="litgleaner-export.zip"',
                "Cache-Control": "no-store",
                "X-Content-Type-Options": "nosniff",
            },
        )

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        try:
            await super().__call__(scope, receive, send)
        finally:
            self._archive.close()


def install_export_routes(app: FastAPI) -> None:
    @app.post("/api/exports/mine")
    def download(ctx: Context, body: MineBody = _EMPTY) -> StreamingResponse:
        try:
            archive = zip_archive(mine(ctx, include_pdfs=body.include_pdfs))
        except OSError as error:
            raise InvalidRequest("导出文件无法生成，请检查可用空间") from error
        return ExportResponse(archive)

    _install_topic_exports(app)


def _download(text: str, filename: str, media_type: str, missing: int = 0) -> Response:
    return Response(
        text,
        media_type=media_type,
        headers={
            "Content-Disposition": f'attachment; filename="{filename}"',
            "Cache-Control": "no-store",
            "X-Content-Type-Options": "nosniff",
            "X-Litgleaner-Needs-Verification": str(missing),
        },
    )


def _install_topic_exports(app: FastAPI) -> None:
    @app.get("/api/topics/{topic_id}/comparison-table")
    def table_view(
        ctx: Context, topic_id: str, tier: str | None = Query(default=None, max_length=1000)
    ) -> TopicComparison:
        return comparison(ctx, topic_id, tier=tier)

    @app.get("/api/topics/{topic_id}/exports/table")
    def table_download(
        ctx: Context,
        topic_id: str,
        format: TableFormat = "md",
        tier: str | None = Query(default=None, max_length=1000),
    ) -> Response:
        result = comparison(ctx, topic_id, tier=tier)
        kind = "text/markdown" if format == "md" else "text/csv"
        return _download(table_text(result, format), f"litgleaner-comparison.{format}", kind)

    @app.get("/api/topics/{topic_id}/references")
    def reference_view(ctx: Context, topic_id: str, only_read: bool = False) -> TopicReferences:
        return references(ctx, topic_id, only_read=only_read)

    @app.get("/api/topics/{topic_id}/exports/refs")
    def refs_download(
        ctx: Context, topic_id: str, format: ReferenceFormat = "bibtex", only_read: bool = False
    ) -> Response:
        result = references(ctx, topic_id, only_read=only_read)
        suffix = "bib" if format == "bibtex" else "csl.json"
        kind = "text/plain" if format == "bibtex" else "application/json"
        return _download(
            reference_text(result, format),
            f"litgleaner-references.{suffix}",
            kind,
            len(result.needs_verification),
        )
