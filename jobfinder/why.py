"""'Why didn't I see this job?' - paste any job link, get the exact reason.

Looks the job up where the scanner keeps things (shown jobs, filtered-out jobs of the last 14
days), and for a careers-page link reads that company's feed now and runs the filters on the job.
Every question is saved (why_cases.json), so the Scan report can show whether it's fixed.
"""
from __future__ import annotations

import json
import re
from datetime import date
from pathlib import Path

from . import discover, filters
from .detect import detect_in_text
from .models import Job
from .sources import FETCHERS
from .util import write_atomic

CASES_FILE = "why_cases.json"
GROUPS = {"main": "Inbox", "maybe": "Maybe", "unchecked": "German not checked", "low": "Low match",
          "research": "Research", "manual": "added by you"}


def _ids(url: str) -> list[str]:
    """Numbers / ids in a link that identify the job (LinkedIn currentJobId, Greenhouse job id ...)."""
    out = re.findall(r"(?:currentJobId=|jobs/view/|/jobs/|jk=|gh_jid=|/job/)([A-Za-z0-9-]{6,})", url)
    return [i for i in out if any(c.isdigit() for c in i)]


def _where(db, url: str) -> dict:
    key = Job(source="", company="", title="", url=url).key
    hit = db.find(url=url.split("?")[0], key=key)
    if hit:
        return hit
    for i in _ids(url):
        for table, tag in (("jobs", "stored"), ("rejected", "rejected")):
            cols = "key, fingerprint, data, first_seen" + (", reason" if table == "rejected" else ", grp, status, status_at")
            row = db.con.execute(f"SELECT {cols} FROM {table} WHERE data LIKE ? LIMIT 1", (f"%{i}%",)).fetchone()
            if row:
                d = dict(row)
                d["data"] = json.loads(d["data"]) if d.get("data") else {}
                return {tag: d}
    return {}


def _from_feed(session, url: str):
    """(job, company entry or None, ats, board) for a careers-page link - read live."""
    found = detect_in_text(url)
    if not found or found[0] not in FETCHERS:
        return None, None, None, None
    ats, board = found
    listed = next((c for c in discover.load_companies(discover_root())
                   if str(c.get("ats", "")).lower() == ats and str(c.get("id", "")).lower() == board.lower()), None)
    name = listed["name"] if listed else board
    try:
        jobs = FETCHERS[ats](session, name, board)
    except Exception:
        return None, listed, ats, board
    ids = _ids(url) or [url.split("?")[0].rstrip("/").rsplit("/", 1)[-1]]
    key = Job(source="", company="", title="", url=url).key
    job = next((j for j in jobs if j.key == key or any(i and i in j.url for i in ids)), None)
    return job, listed, ats, board


_ROOT: list = []


def discover_root() -> Path:
    return _ROOT[0]


def explain(root: Path, db, session, url: str, cfg: dict, judge=None) -> dict:
    _ROOT[:] = [root]
    url = (url or "").strip()
    out = {"url": url, "steps": []}
    hit = _where(db, url)
    if hit:
        _say_where(out, hit)
    site = None
    if ats_link(url):
        job, listed, ats, board = _from_feed(session, url)
    else:
        job, listed, ats, board = None, None, None, None
        if not _job_board_link(url):
            site = _from_website(session, url)       # the company's own site: find the platform behind it
            if site:
                ats, board = site["ats"], site["id"]
                listed = next((c for c in discover.load_companies(root)
                               if str(c.get("ats", "")).lower() == ats and str(c.get("id", "")).lower() == board.lower()), None)
    if job is not None and "verdict" not in out:
        # the same job reached you another way (e.g. from a job board before the company page was added)
        hit = db.find(company=job.company, title=job.title)
        if hit:
            _say_where(out, hit, also=True)
    if site:
        name = listed["name"] if listed else site["name"]
        if not listed:
            _add_auto(root, {"name": name, "ats": ats, "id": board, "added": date.today().isoformat(),
                             "found_via": "you pasted its careers page"})
        de = sum(1 for j in site["jobs"] if _germany(j))
        out["verdict"] = "careers page"
        out["text"] = (f"{name}'s jobs are on {ats.capitalize()} (board \"{board}\"): {len(site['jobs'])} open, "
                       f"{de} in Germany or remote. " + ("It was already on your lists and is read every scan."
                       if listed else "Added - it is read every scan from now on, and its matching jobs reach "
                                      "your tabs in the next scan."))
    elif ats:
        if not listed:
            entry = {"name": (job.company if job else board), "ats": ats, "id": board,
                     "added": date.today().isoformat(), "found_via": "you asked why a job was missing"}
            _add_auto(root, entry)
            out["steps"].append(f"{entry['name']}'s careers page ({ats}) wasn't on your lists - it is now, "
                                f"and is read every scan from now on.")
        if job:
            res = filters.classify([job], cfg["filters"], 3650, judge=judge)
            if res.matched:
                now = "would reach your Inbox"
            elif res.low:
                now = "would go to Low match"
            elif res.maybe:
                now = "would go to Maybe"
            elif res.unchecked:
                now = "would go to German not checked"
            else:
                now = "would still be filtered out: " + (res.rejected[0][1] if res.rejected else "unknown reason")
            out["steps"].append(f"Checked against your filters right now: it {now}.")
            if "verdict" not in out:
                out["verdict"] = "never seen"
                out["text"] = ("The scanner never saw this job" + (" - the company wasn't on your lists." if not listed
                               else " - it may be newer than the last scan."))
        elif listed and "verdict" not in out:
            out["verdict"] = "not in feed"
            out["text"] = "The company's careers feed doesn't list this job (any more) - it may have been taken down."
    if "verdict" not in out:
        out["verdict"] = "never seen"
        if "linkedin." in url:
            out["text"] = ("The scanner never saw this job. LinkedIn is read every 3 hours with keyword searches and "
                           "often blocks automated reading, so it can miss jobs. If the job's apply link goes to the "
                           "company's careers page, paste that link here - the scanner will add the company.")
        elif not ats_link(url) and not _job_board_link(url):
            out["text"] = ("No hiring platform the scanner can read was found behind this page (the jobs may be "
                           "listed only on the company's own site or by email). Jobs from this company still "
                           "reach you when they appear on LinkedIn, Indeed or the other boards.")
        else:
            out["text"] = ("The scanner never saw this job: none of the job boards it reads listed it, and the "
                           "company isn't on your lists. Paste the company's careers-page link to add it.")
    _save_case(root, out)
    return out


def _say_where(out: dict, hit: dict, also: bool = False) -> None:
    if "stored" in hit:
        r = hit["stored"]
        d = r["data"]
        tab = GROUPS.get(r.get("grp"), r.get("grp"))
        st = {"new": "new", "seen": "seen", "saved": "saved", "applied": "applied", "skipped": "marked Not for me"}.get(r.get("status"), r.get("status"))
        out["verdict"] = "shown"
        out["text"] = (f"You were shown it: \"{d.get('title')}\" at {d.get('company')}, found {r['first_seen'][:16].replace('T', ' ')} "
                       f"via {d.get('source')}" + (" (the same job, from another site)" if also else "") +
                       f", in the {tab} tab. Status: {st}.")
        if r.get("status") == "skipped":
            out["text"] += " To get it back: open the Skipped tab and press Back to inbox."
    elif "rejected" in hit:
        r = hit["rejected"]
        d = r["data"]
        out["verdict"] = "filtered"
        out["text"] = (f"The scanner saw it (\"{d.get('title')}\" at {d.get('company')}, via {d.get('source')}, "
                       f"first on {r['first_seen'][:10]}) and filtered it out: {r['reason']}.")


_BOARDS = re.compile(r"linkedin\.|indeed\.|stepstone\.|xing\.|glassdoor\.|arbeitnow\.|arbeitsagentur\.|"
                     r"academics\.|google\.|monster\.|join\.com/|welcometothejungle|wellfound|berlinstartupjobs", re.I)


def _job_board_link(url: str) -> bool:
    return bool(_BOARDS.search(url or ""))


def _germany(job) -> bool:
    from .network import _in_germany
    try:
        return _in_germany(job)
    except Exception:
        return False


def _from_website(session, url: str):
    """A company's own website or careers page -> the hiring platform behind it, with its jobs."""
    from urllib.parse import urlparse
    from .network import careers_from_website, board_name
    if not url.startswith("http"):
        url = "https://" + url
    host = (urlparse(url).hostname or "").removeprefix("www.")
    guess = host.split(".")[0].capitalize() if host else "Company"
    try:
        found = careers_from_website(session, url, guess, guess_names=True)
    except Exception:
        return None
    if not found:
        return None
    try:
        jobs = FETCHERS[found["ats"]](session, guess, found["id"])
    except Exception:
        return None
    shown = re.sub(r"\s*[-|–]?\s*(jobs|careers|karriere)\b.*$", "", board_name(session, found["ats"], found["id"]) or "",
                   flags=re.I).strip()
    name = shown or guess
    for j in jobs:
        j.company = name
    return {"name": name, "ats": found["ats"], "id": found["id"], "jobs": jobs}


def ats_link(url: str) -> bool:
    return bool(detect_in_text(url or ""))


def _add_auto(root: Path, entry: dict) -> None:
    import yaml
    p = root / discover.AUTO_FILE
    old = (yaml.safe_load(p.read_text(encoding="utf-8")) if p.exists() else None) or []
    if any(str(c.get("id", "")).lower() == entry["id"].lower() and c.get("ats") == entry["ats"] for c in old):
        return
    header = ("# Companies Job Radar added by itself: a matching job on a job board linked to\n"
              "# their careers feed. Delete a line to stop reading that company.\n")
    write_atomic(p, header + yaml.safe_dump(old + [entry], allow_unicode=True, sort_keys=False, width=200))


def _save_case(root: Path, out: dict) -> None:
    p = root / CASES_FILE
    try:
        cases = json.loads(p.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        cases = []
    cases = [c for c in cases if c.get("url") != out["url"]][-49:]
    cases.append({"url": out["url"], "asked": date.today().isoformat(), "verdict": out["verdict"],
                  "text": out["text"], "steps": out["steps"]})
    write_atomic(p, json.dumps(cases, indent=1, ensure_ascii=False))


def cases(root: Path) -> list:
    try:
        return json.loads((root / CASES_FILE).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return []
