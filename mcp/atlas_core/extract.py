"""Read the dataset embedded in the published Atlas page.

The page stores its data in ``<script id="atlas-data">`` as gzip + base64 JSON.
The page is the single source of truth: this module only reads it.
"""

from __future__ import annotations

import base64
import gzip
import hashlib
import json
import re
from pathlib import Path

_DATA_RE = re.compile(r'<script id="atlas-data"[^>]*>(.*?)</script>', re.S)


def read_page(path: str | Path) -> tuple[dict, str, str]:
    """Return (dataset, page html, sha256 of the page bytes)."""
    raw = Path(path).read_bytes()
    html = raw.decode("utf-8")
    match = _DATA_RE.search(html)
    if not match:
        raise ValueError(f"{path}: no <script id=\"atlas-data\"> block found")
    payload = gzip.decompress(base64.b64decode(match.group(1).strip()))
    return json.loads(payload), html, hashlib.sha256(raw).hexdigest()


def page_scripts(html: str) -> list[str]:
    """Inline scripts of the page after the data block, in document order."""
    out = []
    for m in re.finditer(r"<script(?![^>]*atlas-data)[^>]*>(.*?)</script>", html, re.S):
        out.append(m.group(1))
    return out
