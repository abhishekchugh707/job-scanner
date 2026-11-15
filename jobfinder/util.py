"""Small helpers: HTTP session, date parsing, HTML to text."""
from __future__ import annotations

import html
import re
from datetime import datetime, timezone
from typing import Any, Optional

import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/130.0 Safari/537.36 job-finder/1.0"
)
TIMEOUT = 20


def make_session() -> requests.Session:
    s = requests.Session()
    # respect_retry_after_header=False: a site answering "429, retry after 3600 s" must not
    # freeze a worker for an hour - retry twice after 1 s and 2 s, then give up on that request
    # (the source is retried next scan, and the catch-up window makes sure nothing is lost).
    retry = Retry(total=2, backoff_factor=1.0,
                  status_forcelist=(429, 500, 502, 503, 504),
                  allowed_methods=("GET", "POST"), respect_retry_after_header=False)
    adapter = HTTPAdapter(max_retries=retry, pool_connections=50, pool_maxsize=50)
    s.mount("https://", adapter)
    s.mount("http://", adapter)
    s.headers.update({"User-Agent": USER_AGENT, "Accept": "application/json, */*"})
    s.hooks["response"].append(_fix_encoding)
    return s


def _fix_encoding(r, *a, **kw):
    """A text page without a charset: requests assumes ISO-8859-1 and "München" becomes
    "MÃ¼nchen". The job sites all send UTF-8, so default to that."""
    ct = r.headers.get("Content-Type", "").lower()
    if "charset" not in ct and (ct.startswith("text/") or "xml" in ct or "json" in ct or not ct):
        r.encoding = "utf-8"
    return r


def parse_date(value: Any) -> Optional[datetime]:
    """Accepts ISO strings, unix seconds or unix milliseconds."""
    if value in (None, ""):
        return None
    try:
        if isinstance(value, (int, float)) or (isinstance(value, str) and value.isdigit()):
            n = float(value)
            if n > 1e11:          # milliseconds
                n /= 1000
            return datetime.fromtimestamp(n, tz=timezone.utc)
        s = str(value).strip().replace("Z", "+00:00")
        if len(s) == 10:          # plain date
            s += "T00:00:00+00:00"
        dt = datetime.fromisoformat(s)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt.astimezone(timezone.utc)
    except (ValueError, OSError, OverflowError):
        return None


_TAG = re.compile(r"<[^>]+>")
_BLOCK = re.compile(r"<(?:br|/?p|/?li|/?ul|/?ol|/?h\d|/?div|/?tr|/?section|/?article|/?blockquote)\b[^>]*>", re.I)


def html_to_text(raw: str) -> str:
    """Plain text with one line per paragraph / list item.

    Line breaks are kept on purpose: the German check reads sentence by sentence,
    and two bullet points ("Very good German" / "Docker is a plus") must not merge.
    """
    if not raw:
        return ""
    text = html.unescape(raw)          # Greenhouse double-escapes its HTML
    text = re.sub(r"<li\b[^>]*>", "\n• ", text, flags=re.I)     # keep bullet points (shown as a list)
    text = _BLOCK.sub("\n", text)
    text = _TAG.sub(" ", text)
    text = html.unescape(text).replace("\xa0", " ")
    text = re.sub(r"[ \t\r\f\v]+", " ", text)
    text = re.sub(r" *\n[\n ]*", "\n", text)
    text = re.sub(r"\n• *\n", "\n• ", text)          # <li><p>text</p></li>
    return text.strip()


def one_line(raw: str) -> str:
    """For titles and locations: html_to_text squashed to a single line."""
    return " ".join(html_to_text(raw).split())


def write_atomic(path, text: str) -> None:
    """Write via a temp file + rename, so a reader (a scan running at the same time) never
    sees half a file. Windows refuses the rename while another process has the file open:
    retry briefly."""
    import os
    import time as _t
    from pathlib import Path as _P
    path = _P(path)
    import threading as _th
    # own temp file per process + thread: two writers at the same moment (scan + background
    # lookup) can't take each other's temp file away
    tmp = path.with_name(f"{path.name}.{os.getpid()}.{_th.get_ident()}.tmp")
    tmp.write_text(text, encoding="utf-8")
    for i in range(20):
        try:
            os.replace(tmp, path)
            return
        except PermissionError:
            _t.sleep(0.1 * (i + 1))
    os.replace(tmp, path)
