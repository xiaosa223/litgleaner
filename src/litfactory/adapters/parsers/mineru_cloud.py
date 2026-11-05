"""MinerU 云端精准解析。

解析可能要几分钟。超时时记下任务号，下次接着查，不重新提交，避免重复消耗额度。
"""

import io
import json
import time
import zipfile
from collections.abc import Callable, Mapping
from pathlib import Path
from typing import Any, Literal

import httpx
from pydantic import Field, ValidationError

from litfactory.adapters.parsers.mineru_api import MinerUClient, TaskNotFound
from litfactory.adapters.parsers.mineru_content_list import to_normalized_document
from litfactory.domain.base import FrozenModel
from litfactory.domain.errors import (
    ConfigError,
    PermanentError,
    SchemaChanged,
    StillProcessing,
)
from litfactory.ports.parser import ParseResult

_MB = 1_000_000
_CONTENT_LIST_SUFFIX = "content_list.json"


class MinerUOptions(FrozenModel):
    state_dir: str
    base_url: str = "https://mineru.net"
    model_version: Literal["vlm", "pipeline"] = "vlm"
    language: str = "ch"
    enable_formula: bool = True
    enable_table: bool = True
    poll_interval_seconds: float = Field(default=5, gt=0)
    timeout_seconds: float = Field(default=600, gt=0)
    max_file_mb: int = Field(default=200, gt=0)
    max_zip_mb: int = Field(default=300, gt=0)
    max_content_list_mb: int = Field(default=50, gt=0)


def _options(raw: Mapping[str, Any]) -> MinerUOptions:
    try:
        return MinerUOptions.model_validate(dict(raw))
    except ValidationError as error:
        raise ConfigError(f"MinerU 配置无效：{error}") from error


def _content_list(archive: bytes, max_bytes: int) -> bytes:
    """只读取需要的 content_list.json，不解压其他文件，也不写入磁盘。"""
    try:
        with zipfile.ZipFile(io.BytesIO(archive)) as zipped:
            members = [
                info
                for info in zipped.infolist()
                if info.filename.endswith(_CONTENT_LIST_SUFFIX) and "/" not in info.filename
            ]
            if not members:
                raise SchemaChanged("MinerU 结果中没有 content_list.json")
            if len(members) > 1:
                raise SchemaChanged("MinerU 结果中有多个 content_list.json，无法确定用哪个")
            member = members[0]
            if member.file_size > max_bytes:
                raise PermanentError(f"content_list.json 过大：{member.file_size} 字节")
            return zipped.read(member)
    except zipfile.BadZipFile as error:
        raise SchemaChanged("MinerU 结果不是有效的压缩包") from error


class MinerUCloudParser:
    def __init__(
        self,
        token: str,
        options: Mapping[str, Any],
        *,
        transport: httpx.BaseTransport | None = None,
        sleep: Callable[[float], None] = time.sleep,
        monotonic: Callable[[], float] = time.monotonic,
        wall_clock: Callable[[], float] = time.time,
    ) -> None:
        if not token:
            raise ConfigError("缺少 MinerU token，请设置环境变量 MINERU_API_TOKEN")
        self._options = _options(options)
        self.parser_id = f"mineru_cloud@{self._options.model_version}"
        self._client = MinerUClient(token, self._options.base_url, transport)
        self._state_dir = Path(self._options.state_dir)
        self._sleep, self._monotonic, self._wall = sleep, monotonic, wall_clock

    def parse(self, pdf: bytes, sha256: str) -> ParseResult:
        if len(pdf) > self._options.max_file_mb * _MB:
            raise PermanentError(f"PDF 大小超过 {self._options.max_file_mb}MB，MinerU 不接受")
        batch_id = self._load_state(sha256) or self._submit(pdf, sha256)
        try:
            entry = self._wait(batch_id)
        except TaskNotFound:
            self._clear_state(sha256)
            raise
        archive = self._client.download(str(entry["full_zip_url"]), self._options.max_zip_mb * _MB)
        raw = _content_list(archive, self._options.max_content_list_mb * _MB)
        try:
            items = json.loads(raw)
        except ValueError as error:
            raise SchemaChanged("content_list.json 不是有效的 JSON") from error
        document = to_normalized_document(items, sha256, self.parser_id)
        self._clear_state(sha256)
        return ParseResult(document, raw)

    def _request(self, sha256: str) -> dict[str, Any]:
        data_id = sha256[:32]
        return {
            "files": [{"name": f"{data_id}.pdf", "data_id": data_id}],
            "model_version": self._options.model_version,
            "language": self._options.language,
            "enable_formula": self._options.enable_formula,
            "enable_table": self._options.enable_table,
        }

    def _submit(self, pdf: bytes, sha256: str) -> str:
        batch_id, upload_url = self._client.request_upload(self._request(sha256))
        self._client.upload(upload_url, pdf)
        self._save_state(sha256, batch_id)  # 上传成功后才记录，未上传的任务不会被续查
        return batch_id

    def _wait(self, batch_id: str) -> Mapping[str, Any]:
        deadline = self._monotonic() + self._options.timeout_seconds
        while True:
            entry = self._client.batch_entry(batch_id)
            state = entry.get("state")
            if state == "done":
                if not entry.get("full_zip_url"):
                    raise SchemaChanged("MinerU 任务已完成但没有结果链接")
                return entry
            if state == "failed":
                reason = self._client.scrub(str(entry.get("err_msg", "")))
                raise PermanentError(f"MinerU 解析失败：{reason}")
            if self._monotonic() >= deadline:
                raise StillProcessing(
                    f"MinerU 仍在处理（{state}），下次运行继续查询，不会重新提交",
                    retry_after_seconds=int(self._options.timeout_seconds),
                )
            self._sleep(self._options.poll_interval_seconds)

    def _state_path(self, sha256: str) -> Path:
        """按模型区分：换模型后不能复用旧模型的任务。"""
        return self._state_dir / f"{sha256}.{self._options.model_version}.json"

    def _load_state(self, sha256: str) -> str | None:
        """续查记录不按时间过期，只有服务端说任务不存在才重新提交，避免重复消耗额度。"""
        path = self._state_path(sha256)
        if not path.is_file():
            return None
        try:
            state = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            state = None
        batch_id = state.get("batch_id") if isinstance(state, dict) else None
        if not isinstance(batch_id, str) or not batch_id:
            path.unlink(missing_ok=True)
            return None
        return batch_id

    def _save_state(self, sha256: str, batch_id: str) -> None:
        self._state_dir.mkdir(parents=True, exist_ok=True)
        path = self._state_path(sha256)
        temporary = path.with_suffix(".tmp")
        state = {"batch_id": batch_id, "submitted_at": self._wall()}
        temporary.write_text(json.dumps(state), encoding="utf-8")
        temporary.replace(path)

    def _clear_state(self, sha256: str) -> None:
        self._state_path(sha256).unlink(missing_ok=True)
