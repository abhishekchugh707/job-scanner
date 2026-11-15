"""Things remembered between runs, so problems and gaps become visible.

- SourceHealth: how many jobs each source returned in recent runs. Warns when a
  source keeps failing or suddenly drops to zero (company changed its careers
  system, feed switched off, ...).
- Suggestions: companies that keep appearing on the job boards / LinkedIn with
  roles for you but are not in companies.yaml yet.
"""
from __future__ import annotations

import json
import re
from datetime import date
from pathlib import Path
from typing import Optional

from .models import Job
from .util import write_atomic

HISTORY = 10          # runs remembered per source
BOARD_SOURCES = {"arbeitsagentur", "arbeitnow", "linkedin", "indeed", "jobspy", "glassdoor", "google"}


def _load(path: Path) -> dict:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (FileNotFoundError, json.JSONDecodeError):
        return {}


class SourceHealth:
    def __init__(self, path: Path):
        self.path = path
        self.data: dict[str, list[int]] = _load(path)

    def record(self, results: dict[str, Optional[int]]) -> None:
        """results: source label -> number of jobs, or None when it failed."""
        for label, n in results.items():
            hist = self.data.get(label, [])
            hist.append(-1 if n is None else int(n))
            self.data[label] = hist[-HISTORY:]

    def warnings(self, current=None, moved=()) -> list[str]:
        """current: labels still in your lists (others - e.g. a company that moved platform and was
        updated - are no longer reported). moved: labels that answer "not found" and are only
        checked once a week."""
        out = []
        for label, hist in sorted(self.data.items()):
            if current is not None and label not in current:
                continue
            if len(hist) >= 2 and hist[-1] == -1 and hist[-2] == -1:
                fails = len(hist) - len(_rstrip(hist, -1))
                if label in moved:
                    out.append(f"{label}: careers page not found {fails} times - the company probably moved "
                               f"to another platform (checked again once a week)")
                else:
                    out.append(f"{label}: failed {fails} runs in a row")
            elif len(hist) >= 4 and hist[-3:] == [0, 0, 0] and any(h > 0 for h in hist[:-3]):
                out.append(f"{label}: no open jobs for 3 scans (had some before - probably just not hiring "
                           f"right now)")
        return out

    def never_had_jobs(self, current=None, min_scans: int = 10) -> list[str]:
        """Careers pages read at least min_scans times that never had a single job (not failures:
        those are warnings). Often just a company that is not hiring - shown as information only."""
        out = []
        for label, hist in sorted(self.data.items()):
            if current is not None and label not in current:
                continue
            if len(hist) >= min_scans and 0 in hist and all(h <= 0 for h in hist):
                out.append(label)
        return out

    def save(self) -> None:
        write_atomic(self.path, json.dumps(self.data, indent=1, ensure_ascii=False))


def _rstrip(hist: list[int], value: int) -> list[int]:
    i = len(hist)
    while i and hist[i - 1] == value:
        i -= 1
    return hist[:i]


_LEGAL = re.compile(r"\b(gmbh|mbh|ag|se|kg|kgaa|co|ug|e\.?\s?v|inc|ltd|llc|bv|sa|sarl|"
                    r"haftungsbeschränkt|niederlassung\b.*|group|holding)\b\.?", re.I)


def norm_company(name: str) -> str:
    n = re.sub(r"\(.*?\)", " ", name.lower())
    n = _LEGAL.sub(" ", n)
    n = re.sub(r"[^a-z0-9äöüß]+", " ", n)
    return " ".join(n.split())


_GENDER = re.compile(
    r"\(\s*(?:[mwfdxi]\s*/\s*)+[mwfdxi]\s*\)|\b(?:[mwfdx]/)+[mwfdx]\b|\*(?:in|innen)\b|"
    r"\(\s*(?:all genders?|alle geschlechter|gn|genderneutral|d)\s*\)|\b(?:all genders|alle geschlechter)\b", re.I)


def norm_title(title: str) -> str:
    """Title without gender tags and punctuation. "(Python)" vs "(Go)" stays different."""
    t = _GENDER.sub(" ", (title or "").lower())
    t = re.sub(r"[^a-z0-9äöüß+#]+", " ", t)
    return " ".join(t.split())


class Suggestions:
    def __init__(self, path: Path, known_companies: list[str]):
        self.path = path
        self.data: dict[str, dict] = _load(path)
        self.known = [k for k in (norm_company(c) for c in known_companies) if k]

    def _is_known(self, key: str) -> bool:
        first = key.split()[0] if key else ""
        for k in self.known:
            if key == k or (len(k) > 3 and k in key) or (len(key) > 3 and key in k):
                return True
            if first and len(first) > 3 and k.split()[0] == first:
                return True
        return False

    def add(self, jobs: list[Job]) -> None:
        today = date.today().isoformat()
        for j in jobs:
            if j.source not in BOARD_SOURCES or not j.company:
                continue
            key = norm_company(j.company)
            if not key or self._is_known(key):
                continue
            e = self.data.setdefault(key, {"name": j.company, "count": 0, "titles": [], "last_seen": today})
            e["count"] += 1
            e["last_seen"] = today
            if j.title not in e["titles"]:
                e["titles"] = (e["titles"] + [j.title])[-3:]

    def top(self, n: int = 15) -> list[dict]:
        live = [e for k, e in self.data.items() if not self._is_known(k)]
        return sorted(live, key=lambda e: (-e["count"], e["name"].lower()))[:n]

    def save(self) -> None:
        write_atomic(self.path, json.dumps(self.data, indent=1, ensure_ascii=False))
