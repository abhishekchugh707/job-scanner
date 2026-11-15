"""Finds companies to read directly.

When a job board (LinkedIn, Indeed, Google Jobs ...) shows a job that matches you and its
apply link goes to a hiring platform the radar can read (Greenhouse, Lever, Personio ...),
that company's careers feed is tested and, if it works, added to companies_auto.yaml.
From then on its jobs reach you straight from its careers page, usually before the boards.
"""
from __future__ import annotations

import json
from datetime import date
from pathlib import Path
from typing import Callable, Optional

import yaml

from .detect import detect_in_text
from .models import Job
from .util import write_atomic
from .sources import FETCHERS
from .tracking import BOARD_SOURCES, norm_company

AUTO_FILE = "companies_auto.yaml"
TRIED_FILE = "companies_auto_tried.json"
MAX_NEW_PER_SCAN = 8
QUEUE_FILE = "companies_lookup_queue.json"


def _read_json(p: Path, default):
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return default


RETRY_DAYS = 30          # a company whose careers page wasn't found is looked up again after this
LOOKUP_VERSION = 3       # bump to look up every earlier miss again (better lookup)


def _due(tried: dict, key: str) -> bool:
    when = tried.get(key)
    if not when:
        return True
    try:
        return (date.today() - date.fromisoformat(str(when)[:10])).days >= RETRY_DAYS
    except ValueError:
        return True


def _find(session, root: Path, item: dict, log) -> Optional[dict]:
    """Careers feed of a company seen on a job board, most reliable route first:
    its own website -> the open-data name index -> the usual board names on each platform."""
    from .network import careers_from_website, find_careers_page
    from . import opendata
    name = item["name"]
    if item.get("site"):
        try:
            res = careers_from_website(session, item["site"], name)
        except Exception:
            res = None
        if res:
            return res
    hit = opendata.lookup(root, name)
    if hit and hit[0] in FETCHERS:
        try:
            FETCHERS[hit[0]](session, name, hit[1])
            return {"name": name, "ats": hit[0], "id": hit[1], "via": "open-data index"}
        except Exception:
            pass
    try:
        res = find_careers_page(session, name)
    except Exception:
        res = None
    if res:
        res["via"] = "name lookup"
    return res


def lookup_queued(session, root: Path, log: Callable[[str], None] = print) -> list[dict]:
    """Background: find the careers page of companies seen on job boards (queued by discover).
    Found ones go to the hourly list."""
    q = _read_json(root / QUEUE_FILE, {})
    tried = _read_json(root / TRIED_FILE, {})
    if tried.get("_v") != LOOKUP_VERSION:
        # the lookup got better: every company it couldn't find before is tried again
        for k in [k for k in tried if k.startswith("name:")]:
            q.setdefault(k[5:], {"name": k[5:], "via": "earlier lookup, tried again"})
        tried["_v"] = LOOKUP_VERSION
        write_atomic(root / TRIED_FILE, json.dumps(tried, indent=1))
        write_atomic(root / QUEUE_FILE, json.dumps(q, indent=1))
    added = []
    for n in list(q):
        item = q.pop(n)
        known = load_companies(root)
        known_ids = {(str(c.get("ats", "")).lower(), str(c.get("id", "")).lower()) for c in known}
        if n in {norm_company(c.get("name", "")) for c in known}:
            write_atomic(root / QUEUE_FILE, json.dumps(q, indent=1))
            continue
        res = _find(session, root, item, log)
        if res and (res["ats"], str(res["id"]).lower()) not in known_ids:
            entry = {"name": item["name"], "ats": res["ats"], "id": res["id"], "added": date.today().isoformat(),
                     "found_via": f"{item.get('via', '')} ({res.get('via', '')})"[:140]}
            p = root / AUTO_FILE
            old = (yaml.safe_load(p.read_text(encoding="utf-8")) if p.exists() else None) or []
            header = ("# Companies Job Radar added by itself: a matching job on a job board linked to\n"
                      "# their careers feed. Delete a line to stop reading that company.\n")
            write_atomic(p, header + yaml.safe_dump(old + [entry], allow_unicode=True, sort_keys=False, width=200))
            added.append(entry)
            log(f"  + found {item['name']}'s careers page ({res['ats']}, via {res.get('via', '?')})"
                f" - read directly from the next scan")
        write_atomic(root / QUEUE_FILE, json.dumps(q, indent=1))
    return added


HOURLY_FILES = ("companies.yaml", AUTO_FILE)                          # your list + found via job boards
DAILY_FILES = ("companies_network.yaml", "companies_discovered.yaml")   # big lists: read once a day


def load_companies(root: Path, files=HOURLY_FILES + DAILY_FILES) -> list[dict]:
    """Every company of the given lists, each tagged with the list it came from ("_list")."""
    out = []
    for name in files:
        p = root / name
        if not p.exists():
            continue
        with p.open(encoding="utf-8") as f:
            for c in yaml.safe_load(f) or []:
                if isinstance(c, dict) and c.get("ats") and c.get("id"):
                    out.append({**c, "_list": name})
    return out


def _platform(job: Job):
    for link in (job.apply_url, job.url):
        found = detect_in_text(link or "")
        if found and found[0] in FETCHERS:
            return found
    return None


def discover(session, jobs: list[Job], companies: list[dict], root: Path,
             log: Callable[[str], None] = print) -> list[dict]:
    known_ids = {(str(c.get("ats", "")).lower(), str(c.get("id", "")).lower()) for c in companies}
    known_names = {norm_company(c.get("name", "")) for c in companies}
    tried_path = root / TRIED_FILE
    try:
        tried = json.loads(tried_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        tried = {}

    todo, by_name = {}, {}
    for j in jobs:
        if j.source not in BOARD_SOURCES or not j.company:
            continue
        found = _platform(j)
        if not found:
            # no careers link in the ad (LinkedIn, Indeed, Arbeitsagentur): look the company up
            # by name on the platforms the radar reads - once per company
            n = norm_company(j.company)
            if n and n not in known_names and _due(tried, f"name:{n}"):
                by_name.setdefault(n, j)
            continue
        ats, board = found
        k = f"{ats}:{board.lower()}"
        if (ats, board.lower()) in known_ids or k in tried or norm_company(j.company) in known_names:
            continue
        todo.setdefault(k, (ats, board, j))

    added = []
    for k, (ats, board, j) in list(todo.items())[:MAX_NEW_PER_SCAN]:
        tried[k] = date.today().isoformat()
        try:
            n = len(FETCHERS[ats](session, j.company, board))
        except Exception as e:
            log(f"  couldn't read {j.company}'s careers feed ({ats}): {str(e)[:60]}")
            continue
        if n:
            entry = {"name": j.company, "ats": ats, "id": board,
                     "added": date.today().isoformat(), "found_via": f"{j.source}: {j.title}"[:120]}
            added.append(entry)
            known_names.add(norm_company(j.company))
            log(f"  + now reading {j.company}'s careers page directly ({ats}, {n} open jobs)")

    # companies seen on job boards without a careers link: queued, looked up by name in the
    # background (lookup_queued), so the scan itself stays fast
    if by_name:
        q = _read_json(root / QUEUE_FILE, {})
        for n, j in by_name.items():
            q.setdefault(n, {"name": j.company, "via": f"{j.source}: {j.title}"[:120], "site": j.company_url or ""})
        write_atomic(root / QUEUE_FILE, json.dumps(q, indent=1))
        for n in by_name:
            tried[f"name:{n}"] = date.today().isoformat()

    write_atomic(tried_path, json.dumps(tried, indent=1))
    if added:
        p = root / AUTO_FILE
        old = []
        if p.exists():
            old = yaml.safe_load(p.read_text(encoding="utf-8")) or []
        header = ("# Companies Job Radar added by itself: a matching job on a job board linked to\n"
                  "# their careers feed. Delete a line to stop reading that company.\n")
        write_atomic(p, header + yaml.safe_dump(old + added, allow_unicode=True, sort_keys=False, width=200))
    return added
