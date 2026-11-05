"""稳定哈希：同样的内容在任何机器上都得到同样的指纹。"""

import hashlib
import json
from typing import Any

HASH_LENGTH = 16


def canonical_json(value: Any) -> str:
    return json.dumps(value, sort_keys=True, ensure_ascii=False, separators=(",", ":"))


def stable_hash(value: Any) -> str:
    digest = hashlib.sha256(canonical_json(value).encode("utf-8"))
    return digest.hexdigest()[:HASH_LENGTH]
