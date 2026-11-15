"""Finds companies hiring for your roles in Germany from an open job dataset.

github.com/Riley-Dorrington/job-board-aggregator scrapes ~1.4M jobs every day straight from
company careers platforms (Greenhouse, Lever, Ashby, Workday ...) and publishes them as open
data (code MIT, company data CC BY-NC 4.0 - fine for personal use). Once a week this module
reads that data, keeps jobs located in Germany whose title fits your role words, and adds
their companies to companies_discovered.yaml. From then on the radar reads those companies'
careers feeds itself, every hour - so the dataset is only used to find companies, never as
a job source (it's a day behind; the radar reads live).
"""
from __future__ import annotations

import gzip
import json
import re
from collections import Counter, defaultdict
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Callable

import yaml

from . import places
from .detect import detect_in_text
from .filters import excluded_word, has_role_word
from .models import Job
from .sources import FETCHERS
from .tracking import norm_company
from .util import write_atomic

BASE = "https://riley-dorrington.github.io/job-board-data/data/chunks"
OUT_FILE = "companies_discovered.yaml"
STATE_FILE = "opendata_state.json"
ATS = {"greenhouse": "greenhouse", "lever": "lever", "ashby": "ashby", "workday": "workday"}
VERSION = 5            # bump when the selection changes: the list is rebuilt on the next start
INDEX_FILE = "opendata_index.json"   # company name -> careers feed, for every company in the dataset


def in_germany(location: str) -> bool:
    """An office in Germany ("Berlin, CT" is not; "Berlin · London" is)."""
    for o in places.offices(location or ""):
        if places.office_country(o) == "foreign":
            continue
        if re.search(r"\b(germany|deutschland)\b", o, re.I) or places.office_country(o) == "DE":
            return True
    return False


def company_of(rec: dict):
    """(ats, board id) for a dataset record, or None if the radar can't read that platform."""
    ats = ATS.get(str(rec.get("ats", "")).lower())
    if not ats:
        return None
    found = detect_in_text(rec.get("url") or "")          # the board id exactly as in the link
    if ats == "workday":
        return found if found and found[0] == "workday" else None
    if found and found[0] == ats:                          # (Ashby ids are case-sensitive)
        return found
    slug = str(rec.get("company_slug") or rec.get("company") or "").strip()
    return (ats, slug) if slug and "|" not in slug else None


def _dead_boards(root: Path) -> set:
    """Boards the scans found gone (moved platform): (ats, id) pairs."""
    try:
        from .db import JobDB
        db = JobDB(root / "jobs.db")
        dead = db.get("dead_boards") or {}
        db.con.close()
    except Exception:
        return set()
    return {tuple(k.lower().split(":", 1)) for k, v in dead.items() if v.get("n", 0) >= 3 and ":" in k}


_INDEX: dict = {"mtime": None, "data": {}}
_LEGAL_END = re.compile(r"(gmbh|ag|se|inc|ltd|llc|bv|kg|ug|co)+$")


def squash(name: str) -> str:
    """"ARX Robotics GmbH", "arx-robotics" and "arxroboticsgmbh" -> "arxrobotics"."""
    n = re.sub(r"[^a-z0-9]", "", norm_company(name or "").replace("ä", "ae").replace("ö", "oe")
               .replace("ü", "ue").replace("ß", "ss"))
    n2 = _LEGAL_END.sub("", n)
    return n2 if len(n2) >= 3 else n


def lookup(root: Path, name: str):
    """(ats, id) of a company's careers feed from the dataset's name index, or None."""
    p = root / INDEX_FILE
    try:
        m = p.stat().st_mtime
    except OSError:
        return None
    if _INDEX["mtime"] != m:
        try:
            _INDEX.update(mtime=m, data=json.loads(p.read_text(encoding="utf-8")))
        except (OSError, ValueError):
            return None
    data = _INDEX["data"]
    hit = data.get(norm_company(name or "")) or data.get(squash(name))
    return tuple(hit) if hit else None


class OpenData:
    def __init__(self, root: Path, session_factory: Callable, log: Callable[[str], None] = print,
                 every_days: float = 7):
        self.root, self.session_factory, self.log, self.every = root, session_factory, log, every_days
        self.running = False
        self.progress = {"chunks": 0, "total": 0, "found": 0}

    def _state(self) -> dict:
        try:
            return json.loads((self.root / STATE_FILE).read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return {}

    def due(self) -> bool:
        st = self._state()
        if st.get("v") != VERSION:
            return True
        last = st.get("last_run")
        return not last or datetime.now(timezone.utc) - datetime.fromisoformat(last) > timedelta(days=self.every)

    def run(self, filters_cfg: dict, known: list[dict]) -> list[dict]:
        if self.running or not self.due():
            return []
        self.running = True
        try:
            return self._run(filters_cfg, known)
        except Exception as e:
            self.log(f"Open-data company search failed: {str(e)[:120]} (tries again next week)")
            return []
        finally:
            self.running = False

    def _run(self, cfg: dict, known: list[dict]) -> list[dict]:
        s = self.session_factory()
        manifest = s.get(f"{BASE}/jobs_manifest.json", timeout=30).json()
        chunks = manifest.get("chunks") or []
        self.progress = {"chunks": 0, "total": len(chunks), "found": 0}
        self.log(f"Open data: looking through {manifest.get('totalJobs', 0):,} jobs for companies "
                 f"hiring your roles in Germany...")
        hits: dict = defaultdict(Counter)            # (ats, id) -> titles
        index: dict = {}
        names: dict = {}
        for name in chunks:
            try:
                r = s.get(f"{BASE}/{name}", timeout=120)
                recs = json.loads(gzip.decompress(r.content))
            except Exception as e:
                self.log(f"  skipped {name}: {str(e)[:80]}")
                continue
            for rec in recs:
                key = company_of(rec)
                if key and key[0] in FETCHERS:                # name -> careers feed, every company
                    for nm in (rec.get("company"), rec.get("company_slug"), key[1] if key[0] != "workday" else ""):
                        for n in {norm_company(str(nm or "")), squash(str(nm or ""))}:
                            if n and n not in index:
                                index[n] = list(key)
                if not in_germany(rec.get("location") or ""):
                    continue
                j = Job(source="opendata", company=str(rec.get("company") or ""), title=str(rec.get("title") or ""),
                        url=str(rec.get("url") or ""))
                if excluded_word(j, cfg) or not has_role_word(j, cfg):
                    continue
                key = company_of(rec)
                if key:
                    hits[key][j.title] += 1
                    names.setdefault(key, j.company)
            self.progress["chunks"] += 1

        write_atomic(self.root / INDEX_FILE, json.dumps(index, separators=(",", ":")))
        p = self.root / OUT_FILE
        mine = {(str(c.get("ats", "")).lower(), str(c.get("id", "")).lower())
                for c in ((yaml.safe_load(p.read_text(encoding="utf-8")) if p.exists() else None) or [])}
        others = [c for c in known if (str(c.get("ats", "")).lower(), str(c.get("id", "")).lower()) not in mine]
        known_ids = {(str(c.get("ats", "")).lower(), str(c.get("id", "")).lower()) for c in others}
        known_names = {norm_company(c.get("name", "")) for c in others}
        known_ids |= _dead_boards(self.root)       # "not found" 3+ times: the company moved, don't re-add it
        new = []
        for (ats, board), titles in sorted(hits.items(), key=lambda kv: -sum(kv[1].values())):
            if (ats, board.lower()) in known_ids or ats not in FETCHERS:
                continue
            name = names[(ats, board)]
            if ats != "workday":
                name = name.replace("-", " ").replace("_", " ").strip()
                name = name if any(ch.isupper() for ch in name) else name.title()
            if norm_company(name) in known_names:
                continue
            from .filters import excluded_company
            if excluded_company(Job(source="opendata", company=name, title="", url=""), cfg):
                continue                      # a company you excluded in config.yaml
            new.append({"name": name, "ats": ats, "id": board, "added": date.today().isoformat(),
                        "why": f"{sum(titles.values())} matching jobs in Germany, e.g. {titles.most_common(1)[0][0]}"[:140]})
        self.progress["found"] = len(new)

        # rebuilt every time from the current dataset: companies that stopped hiring drop out
        header = ("# Companies hiring your roles in Germany, found in the open job dataset of\n"
                  "# github.com/Riley-Dorrington/job-board-aggregator (refreshed weekly). The radar reads\n"
                  "# their careers feeds itself every scan. Delete a line to stop reading a company.\n")
        write_atomic(p, header + yaml.safe_dump(new, allow_unicode=True, sort_keys=False, width=200))
        st = self._state()
        st.update(v=VERSION, last_run=datetime.now(timezone.utc).isoformat(timespec="seconds"),
                  dataset=manifest.get("last_updated"), added=len(new), companies_in_germany=len(hits))
        write_atomic(self.root / STATE_FILE, json.dumps(st, indent=1))
        self.log(f"Open data: {len(hits)} companies hiring your roles in Germany; {len(new)} not in your other lists")
        return new
