"""把应用层的翻译事件转成 SSE。连接建立后出错只能用 error 事件告知，状态码已经发出去了。"""

import json
import logging
from collections.abc import Iterator
from dataclasses import asdict

from fastapi.responses import StreamingResponse

from litgleaner.application.translation import (
    BlockTranslation,
    TranslateEvent,
    TranslationDelta,
    TranslationStatus,
)
from litgleaner.domain.errors import LitGleanerError

logger = logging.getLogger(__name__)


def _event(name: str, data: object) -> str:
    return f"event: {name}\ndata: {json.dumps(data, ensure_ascii=False)}\n\n"


def _sse(events: Iterator[TranslateEvent]) -> Iterator[str]:
    try:
        for event in events:
            if isinstance(event, TranslationDelta):
                yield _event("delta", {"index": event.index, "text": event.text})
            elif isinstance(event, TranslationStatus):
                yield _event("status", {"state": event.state})
            elif isinstance(event, BlockTranslation):
                yield _event("block", asdict(event))
        yield _event("end", {})
    except LitGleanerError as error:  # 预算用完、参数不对、模型出错：都告诉界面，不中断服务
        yield _event("error", {"message": str(error)})
    except Exception:
        logger.exception("翻译时出现程序错误")
        yield _event("error", {"message": "翻译时出现程序错误，详情见服务日志"})


def sse_response(events: Iterator[TranslateEvent]) -> StreamingResponse:
    return StreamingResponse(
        _sse(events),
        media_type="text/event-stream",
        headers={"X-Accel-Buffering": "no"},  # 部署在反向代理后面时不要缓冲
    )
