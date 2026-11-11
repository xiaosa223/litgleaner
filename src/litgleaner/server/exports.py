"""个人数据以下载包返回，不接受网页指定本机写入路径。"""

from collections.abc import Iterator
from typing import BinaryIO

from fastapi import FastAPI
from fastapi.responses import StreamingResponse
from pydantic import Field
from starlette.types import Receive, Scope, Send

from litgleaner.application.export import mine
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
