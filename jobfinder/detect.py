"""Work out which hiring platform a careers page uses, and its board id."""
from __future__ import annotations

import re
from typing import Optional

PATTERNS = [
    ("greenhouse", r"(?:boards|job-boards)(?:\.eu)?\.greenhouse\.io/(?:embed/job_board\?for=)?([A-Za-z0-9_-]+)"),
    ("greenhouse", r"boards-api\.greenhouse\.io/v1/boards/([A-Za-z0-9_-]+)"),
    ("greenhouse", r"greenhouse\.io/embed/job_board/js\?for=([A-Za-z0-9_-]+)"),
    ("lever", r"jobs\.(?:eu\.)?lever\.co/([A-Za-z0-9._-]+)"),
    ("ashby", r"jobs\.ashbyhq\.com/([A-Za-z0-9%._-]+)"),
    ("personio", r"([a-z0-9-]+)\.jobs\.personio\.(?:de|com)"),
    ("smartrecruiters", r"(?:careers|jobs)\.smartrecruiters\.com/([A-Za-z0-9_-]+)"),
    ("recruitee", r"([a-z0-9-]+)\.recruitee\.com"),
    ("workable", r"apply\.workable\.com/([A-Za-z0-9_-]+)"),
    ("join", r"join\.com/companies/([A-Za-z0-9._-]+)"),
    ("workday", r"(https://[\w-]+\.wd[\w-]*\.myworkdayjobs\.com/(?:[a-z]{2}-[A-Z]{2}/)?[^/?#\"'\s]+)"),
    # Eightfold: https://jobs.ericsson.com/careers/job/563121777546816 (15+ digit position ids)
    ("eightfold", r"https://([\w.-]+)/careers/job/\d{12,}"),
]
IGNORE_IDS = {"embed", "api", "v1", "jobs", "job", "j", "apply", "static", "assets", "careers",
              "www", "wday"}
LOWERCASE = {"personio", "recruitee", "join", "eightfold"}     # subdomain-style ids are lowercase


def detect_in_text(text: str) -> Optional[tuple[str, str]]:
    for ats, pat in PATTERNS:
        for m in re.finditer(pat, text, flags=re.I if ats in LOWERCASE else 0):
            board = m.group(1).strip("/")
            if ats in LOWERCASE:
                board = board.lower()
            if board.lower() not in IGNORE_IDS:
                return ats, board
    return None


def detect(session, url: str) -> Optional[tuple[str, str]]:
    found = detect_in_text(url)
    if found:
        return found
    r = session.get(url, timeout=20, headers={"Accept": "text/html"})
    r.raise_for_status()
    return detect_in_text(r.url) or detect_in_text(r.text)
