"""手写最小 PDF，测试不依赖外部文件。"""


def _escape(text: str) -> str:
    return text.replace("\\", "\\\\").replace("(", "\\(").replace(")", "\\)")


Line = str | tuple[str, float, float] | tuple[str, float, float, float]


def _content(lines: list[Line]) -> bytes:
    """字符串行接着上一行往下排；(文字, x, y[, 缩放]) 把这一行放到指定位置，用来模拟分栏。"""
    body = ["BT", "/F1 10 Tf", "12 TL", "72 760 Td"]
    for line in lines:
        if isinstance(line, tuple):
            text, x, y, *rest = line
            k = rest[0] if rest else 1
            body.append(f"{k} 0 0 {k} {x} {y} Tm ({_escape(text)}) Tj T*")
        else:
            body.append(f"({_escape(line)}) Tj T*")
    body.append("ET")
    return "\n".join(body).encode("latin-1")


def make_pdf(pages: list[list[Line]], rotate: int = 0) -> bytes:
    objects: list[bytes] = [
        b"<< /Type /Catalog /Pages 2 0 R >>",
        b"",  # Pages，最后填
        b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>",
    ]
    kids = []
    for lines in pages:
        stream = _content(lines)
        objects.append(b"<< /Length %d >>\nstream\n" % len(stream) + stream + b"\nendstream")
        content_id = len(objects)
        objects.append(
            b"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792] /Rotate %d "
            b"/Resources << /Font << /F1 3 0 R >> >> /Contents %d 0 R >>" % (rotate, content_id)
        )
        kids.append(f"{len(objects)} 0 R")
    objects[1] = f"<< /Type /Pages /Kids [{' '.join(kids)}] /Count {len(kids)} >>".encode()
    out = bytearray(b"%PDF-1.4\n")
    offsets = []
    for number, body in enumerate(objects, start=1):
        offsets.append(len(out))
        out += b"%d 0 obj\n" % number + body + b"\nendobj\n"
    xref = len(out)
    out += b"xref\n0 %d\n0000000000 65535 f \n" % (len(objects) + 1)
    for offset in offsets:
        out += b"%010d 00000 n \n" % offset
    out += b"trailer\n<< /Size %d /Root 1 0 R >>\nstartxref\n%d\n%%%%EOF\n" % (
        len(objects) + 1,
        xref,
    )
    return bytes(out)
