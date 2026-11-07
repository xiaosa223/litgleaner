"""假发布渠道：记录发送内容，可选写入文件，方便查看演示结果。"""

import json
from pathlib import Path

from litgleaner.ports.publisher import Receipt, Rendered


class FakePublisher:
    def __init__(self, output_file: Path | None = None) -> None:
        self._output = output_file
        self.sent: list[Rendered] = []
        self.fail_next: list[Exception] = []
        self.crash_after_send = False

    def publish(self, rendered: Rendered) -> Receipt:
        if self.fail_next:
            raise self.fail_next.pop(0)
        self.sent.append(rendered)
        if self._output:
            self._output.parent.mkdir(parents=True, exist_ok=True)
            with self._output.open("a", encoding="utf-8") as handle:
                record = {"title": rendered.title, "body": rendered.body}
                handle.write(json.dumps(record, ensure_ascii=False) + "\n")
        if self.crash_after_send:
            raise ConnectionError("连接中断，无法确认对方是否收到")
        return Receipt(external_id=f"fake-{len(self.sent)}")
