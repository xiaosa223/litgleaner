"""假全文来源：标题含 NoPDF 时返回没有全文。"""

import json

from litgleaner.ports.fulltext import FullText, FullTextRequest


class FakeFullText:
    name = "fake_fulltext"

    def fetch(self, request: FullTextRequest) -> FullText | None:
        if "NoPDF" in request.title:
            return None
        body = json.dumps({"title": request.title}).encode("utf-8")
        return FullText(b"%PDF-1.7\n" + body, "open", self.name)
