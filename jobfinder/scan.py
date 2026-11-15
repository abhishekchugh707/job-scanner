"""One scan: read every source, filter, store new jobs in jobs.db.

Used by app.py (hourly, in the background) and by run.py (by hand).
"""
from __future__ import annotations

import sys
import re
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Callable, Optional

import yaml

from . import discover, enrich, filters, network, places, report
from .db import JobDB, fingerprint
from .models import Job, SOURCE_RANK, source_rank
from .sources import BOARDS, FETCHERS
from .sources.boards import is_academic
from .tracking import SourceHealth, Suggestions, norm_company, norm_title
from .util import write_atomic, make_session

ROOT = Path(__file__).resolve().parent.parent
DEFAULT_HOME = {"name": "Köln", "lat": 50.9375, "lon": 6.9603, "near_km": 80}


def load_yaml(name: str, root: Path = ROOT):
    with (root / name).open(encoding="utf-8") as f:
        return yaml.safe_load(f)


FULL_LIST_FEEDS = {"greenhouse", "lever", "ashby", "personio", "recruitee", "workable", "smartrecruiters", "join"}


def company_id(c: dict) -> str:
    return f"{str(c.get('ats', '')).lower()}:{str(c.get('id', '')).lower()}"


# How many feeds of one platform are read at the same time. Ashby and Workable answer
# "429 Too Many Requests" quickly; Greenhouse and Lever take a lot (numbers as used by
# github.com/Riley-Dorrington/job-board-aggregator, which reads 20,000+ companies daily).
PLATFORM_LIMITS = {"eightfold": 4, "greenhouse": 12, "lever": 12, "workday": 8, "ashby": 4, "workable": 2, "smartrecruiters": 4,
                   "personio": 8, "recruitee": 6, "join": 4, "page": 6}


def _limited(fn, sem):
    def run(*a):
        with sem:
            return fn(*a)
    return run


def fetch_all(companies, boards_cfg, use_boards: bool, workers: int, skip_boards=(), windows=None):
    """windows: {source key: (days, reason)} - sources whose older jobs count this time: a company
    read for the first time, or a source the radar couldn't read for a while (catch-up)."""
    windows = windows or {}
    import threading
    session = make_session()
    sems = {k: threading.Semaphore(v) for k, v in PLATFORM_LIMITS.items()}
    tasks = {}
    with ThreadPoolExecutor(max_workers=max(workers, 48)) as pool:
        for c in companies:
            ats = str(c.get("ats", "")).lower()
            fn = FETCHERS.get(ats)
            if fn and ats in sems:
                fn = _limited(fn, sems[ats])
            if not fn:
                print(f"  ! {c.get('name')}: unknown ats '{c.get('ats')}'", file=sys.stderr)
                continue
            tasks[pool.submit(fn, session, c["name"], str(c["id"]))] = (f"{c['name']} ({c['ats']})",
                                                                         company_id(c))
        if use_boards:
            for name, cfg in (boards_cfg or {}).items():
                if cfg and cfg.get("enabled") and name not in skip_boards:
                    tasks[pool.submit(BOARDS[name], session, cfg)] = (name, "board:" + name)
        jobs, ok, errors, per_source, ok_keys = [], 0, [], {}, set()
        for fut in as_completed(tasks):
            label, key = tasks[fut]
            try:
                got = fut.result()
                win = windows.get(key)
                for j in got:
                    j.origin = label
                    if win:
                        j.max_age_days, j.window_reason = win
                jobs.extend(got)
                ok += 1
                per_source[label] = len(got)
                ok_keys.add(key)
            except Exception as e:                       # one broken source never stops the run
                errors.append((label, str(e)[:160]))
                per_source[label] = None
    return jobs, ok, errors, per_source, ok_keys


_RANK = SOURCE_RANK       # kept under its old name


def dedupe(jobs: list[Job]) -> list[Job]:
    """Same role on a company feed and a job board -> keep the company feed copy."""
    best: dict[tuple, Job] = {}
    for j in jobs:
        k = (norm_company(j.company)[:20], norm_title(j.title))
        cur = best.get(k)
        if cur is None or _RANK.get(j.source, 3) < _RANK.get(cur.source, 3) or (
                _RANK.get(j.source, 3) == _RANK.get(cur.source, 3) and len(j.description) > len(cur.description)):
            best[k] = j
    return list(best.values())


YIELD_DAYS = 30


def keyword_yield(db, jobs: list[Job], groups: dict, cfg: dict, today: str) -> list[dict]:
    """Per job-board search (site + keyword), over the last 30 days: how many jobs it found that
    reached your tabs, and how many of those nothing else found (no other search, no careers page).
    A search that never finds anything only you'd otherwise miss can be dropped."""
    reached = {fingerprint(j.company, j.title) for g, lst in groups.items() if g not in ("low", "india_low") for j in lst}
    finders: dict = {}
    for j in jobs:
        fp = fingerprint(j.company, j.title)
        finders.setdefault(fp, set()).update(j.found_by or ["feed:" + (j.origin or j.source)])
    data = db.get("keyword_yield") or {}
    boards = cfg.get("boards") or {}
    names = {"linkedin": "LinkedIn", "indeed": "Indeed", "naukri": "Naukri", "google": "Google"}
    for bname, region in (("jobspy", ""), ("jobspy_india", " India"), ("jobspy_india_fast", " India")):
        b = boards.get(bname) or {}
        if not b.get("enabled"):
            continue
        sites = b.get("sites") or []
        broad = [s for s in sites if s not in (b.get("broad_skip_sites") or ["linkedin"])]
        for kw, ss in [(k, sites) for k in b.get("keywords", [])] + [(k, broad) for k in b.get("broad_keywords", [])]:
            for site in ss:
                data.setdefault(f"{names.get(site, site)}{'' if site == 'naukri' else region}: {kw}",
                                {"since": today, "reached": {}, "unique": {}})
    ba = boards.get("arbeitsagentur") or {}
    if ba.get("enabled"):
        for kw in ba.get("keywords", []):
            data.setdefault(f"Arbeitsagentur: {kw}", {"since": today, "reached": {}, "unique": {}})
    for fp in reached:
        for f in finders.get(fp, ()):
            if f.startswith("feed:"):
                continue
            e = data.setdefault(f, {"since": today, "reached": {}, "unique": {}})
            e["reached"].setdefault(fp, today)
            if finders[fp] == {f}:
                e["unique"].setdefault(fp, today)
    cutoff = (datetime.fromisoformat(today) - timedelta(days=YIELD_DAYS)).date().isoformat()
    for e in data.values():
        for k in ("reached", "unique"):
            e[k] = {fp: d for fp, d in e[k].items() if d >= cutoff}
    db.put("keyword_yield", data)
    rows = [{"search": f, "since": e.get("since", today), "reached": len(e["reached"]), "unique": len(e["unique"])}
            for f, e in data.items()]
    return sorted(rows, key=lambda r: (-r["unique"], -r["reached"], r["search"]))


def home_of(cfg: dict) -> dict:
    return {**DEFAULT_HOME, **(cfg.get("home") or {})}


INDIA_VERSION = 1       # bump when an India rule should also apply to the jobs already in the India tab


def refresh_india(db, fcfg: dict) -> int:
    """Apply the current India rules (.NET -> LOW, priority cities/states) to stored open jobs."""
    import json as _json
    from .db import _LOCK
    changed = 0
    with _LOCK, db.con:
        for row in db.con.execute("SELECT key, grp, status, data FROM jobs WHERE grp IN ('india','india_low')").fetchall():
            d = _json.loads(row["data"] or "{}")
            j = Job(source=d.get("source", ""), company=d.get("company", ""), title=d.get("title", ""),
                    url=d.get("url", ""), location=d.get("location") or "", description=d.get("description") or "")
            grp, status = row["grp"], row["status"]
            prio = filters.india_priority(j, fcfg)
            if grp == "india" and filters.india_dotnet(j):
                grp, status = "india_low", ("seen" if status == "new" else status)
                d["flags"] = (d.get("flags") or []) + [".NET job - moved to LOW"]
            if grp != row["grp"] or prio != d.get("prio"):
                d["prio"] = prio
                db.con.execute("UPDATE jobs SET grp=?, status=?, data=? WHERE key=?",
                               (grp, status, _json.dumps(d, ensure_ascii=False), row["key"]))
                changed += 1
    return changed


def india_lane(boards: dict) -> None:
    """The fast India lane (Naukri/Indeed, hourly) uses the same keywords, places and window as
    the LinkedIn India lane unless it has its own."""
    slow, fast = boards.get("jobspy_india") or {}, boards.get("jobspy_india_fast")
    if fast:
        for k in ("keywords", "broad_keywords", "locations", "hours_old", "results"):
            if k not in fast and k in slow:
                fast[k] = slow[k]


FILTER_VERSION = 5      # bump when a filter fix should also clean the jobs already in the list


BG_ERRORS = "background_errors.json"


def record_background_error(root: Path, where: str, e: Exception) -> None:
    """Errors in background work (company lookups, the skills model) - shown with the next scan."""
    import json as _json
    p = root / BG_ERRORS
    try:
        errs = _json.loads(p.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        errs = []
    errs = (errs + [{"at": datetime.now().strftime("%H:%M"), "where": where,
                     "error": f"{type(e).__name__}: {str(e)[:160]}"}])[-20:]
    write_atomic(p, _json.dumps(errs, indent=1, ensure_ascii=False))


def health_warnings(root: Path, cfg: dict, model, model_stats: dict, fetch_report: dict,
                    linkedin_read: bool, india_read: bool = False) -> list[str]:
    """Things that would otherwise go wrong quietly. Each one falls back safely, but you should know."""
    import json as _json
    import time as _t
    out = []
    if (cfg.get("skills_model") or {}).get("enabled", True) and model is not None and not model.ready:
        if not model.loading:
            out.append(f"Skills model is {model.status} - titles without your role words are judged on keywords "
                       f"only{' (retrying automatically)' if model.attempts else ''}")
        elif model.load_started and _t.time() - model.load_started > 15 * 60:
            out.append("Skills model has been loading for over 15 minutes - titles without your role words are "
                       "judged on keywords only until it's ready")
    if model_stats.get("errors"):
        out.append(f"Skills model failed on {model_stats['errors']} jobs this scan ({model_stats.get('first_error', '')})"
                   f" - those were judged on keywords only")
    p = root / BG_ERRORS
    try:
        for e in _json.loads(p.read_text(encoding="utf-8")):
            out.append(f"Background {e['where']} crashed at {e['at']}: {e['error']} - it runs again after the next scan")
        p.unlink()
    except (OSError, ValueError):
        pass
    if fetch_report.get("failed", 0) >= 3:
        out.append(f"Couldn't read the description of {fetch_report['failed']} jobs this scan - they're kept "
                   f"(Low match / German not checked) so you can check them yourself")
    from .sources.boards import LAST_JOBSPY
    if linkedin_read and LAST_JOBSPY and "linkedin" in LAST_JOBSPY and LAST_JOBSPY["linkedin"] == 0:
        out.append("LinkedIn returned no jobs this time (it blocks automated reads now and then) - its companies' "
                   "careers pages and the other boards are still read")
    if india_read:
        from .sources.boards import LAST_JOBSPY_INDIA, jobspy_has_naukri
        wanted = [s for s in LAST_JOBSPY_INDIA if not s.startswith("_")]
        names = {"linkedin": "LinkedIn India", "naukri": "Naukri", "indeed": "Indeed India"}
        if "naukri" in wanted and not jobspy_has_naukri():
            out.append("Naukri can't be read: the installed python-jobspy has no Naukri reader and couldn't be "
                       "updated (run 'pip install -U python-jobspy' once) - LinkedIn/Indeed India still are")
        for site in wanted:
            if site in LAST_JOBSPY_INDIA and LAST_JOBSPY_INDIA[site] == 0:
                out.append(f"{names.get(site, site)} returned no jobs this time (probably blocked for a while) - "
                           f"the other India sources are still read")
    return out


def daily_due(db: JobDB, cfg: dict, now_local: Optional[datetime] = None) -> bool:
    """The big lists are read in the first scan of the day after daily_scan_hour (local time),
    or whenever their last full read is more than 24 h ago (PC was off in the morning...)."""
    now_local = now_local or datetime.now().astimezone()
    last = db.get("daily_last_ok")
    if not last:
        return True
    last_local = datetime.fromisoformat(last).astimezone(now_local.tzinfo)
    if (now_local - last_local).total_seconds() > 24 * 3600:
        return True
    hour = int((cfg.get("daily_scan_hour") if cfg.get("daily_scan_hour") is not None else 7))
    return last_local.date() < now_local.date() and now_local.hour >= hour


def run_scan(root: Path = ROOT, days: Optional[float] = None, use_boards: bool = True,
             force_linkedin: bool = False, log: Callable[[str], None] = print, full: Optional[bool] = None):
    """Runs one scan and returns a summary. New jobs land in jobs.db as status 'new'.
    full=None: the daily lists are included when due; True/False forces it."""
    t0 = time.time()
    cfg = load_yaml("config.yaml", root)
    workers = int(cfg.get("parallel_requests", 16))
    db = JobDB(root / "jobs.db")
    db.import_seen_file(root / "seen_jobs.json")
    # a filter fix applies to jobs already in the list too (e.g. "Pune, Gera" was let through as Germany)
    if db.get("filter_version") != FILTER_VERSION:
        def keep(d):
            j = Job(source=d.get("source", ""), company=d.get("company", ""), title=d.get("title", ""),
                    url=d.get("url", ""), location=d.get("location") or "")
            if re.match(r"^\d+\s+(locations|standorte)$", j.location.strip(), re.I):
                return False                      # unresolved Workday "2 Locations": re-read properly
            if "capitalone.wd" in j.url and j.location.endswith("(Germany)"):
                return False                      # Delaware ("DE") was taken for Germany's filter
            if "Ã" in j.title + j.location + j.company:
                return False                      # stored with broken characters: re-read cleanly
            if any(str(f).startswith("found by skills") for f in d.get("flags") or []) \
                    and not filters.tech_skills(d.get("matched") or [], cfg["filters"]):
                return False                      # names none of your technologies ("Energie" only)
            if filters.excluded_company(j, cfg["filters"]):
                return False                      # a company you excluded (e.g. sggovterp)
            return filters.location_ok(j, cfg["filters"])
        n = db.drop_open_jobs(keep)
        db.put("filter_version", FILTER_VERSION)
        if n:
            log(f"Removed {n} jobs that an older filter had let through")
    if not cfg["filters"].get("india"):
        n = db.delete_groups(("india", "india_low"))      # India switched off: its stored jobs go too
        if n:
            log(f"India is switched off: removed {n} stored India jobs")
    elif db.get("india_version") != INDIA_VERSION:
        n = refresh_india(db, cfg["filters"])
        db.put("india_version", INDIA_VERSION)
        if n:
            log(f"India tab: {n} stored jobs re-sorted (.NET jobs to LOW, priority states first)")
    daily = daily_due(db, cfg) if full is None else full
    companies = discover.load_companies(root, discover.HOURLY_FILES + (discover.DAILY_FILES if daily else ()))
    if not daily:
        # a company just added to a daily list is read in the next scan, not tomorrow morning
        seen_ids = set(db.get("backfilled") or [])
        companies += [c for c in discover.load_companies(root, discover.DAILY_FILES) if company_id(c) not in seen_ids]
    # careers pages that answered "not found" 3 scans in a row: checked once a week, not every scan
    # (the board moved or the company left that platform; same idea as the "dead slug" cache of
    # github.com/Riley-Dorrington/job-board-aggregator)
    dead = db.get("dead_boards") or {}
    week_ago = (datetime.now(timezone.utc) - timedelta(days=7)).isoformat()
    companies = [c for c in companies if not (dead.get(company_id(c), {}).get("n", 0) >= 3
                                              and dead[company_id(c)].get("last", "") > week_ago)]
    n_daily = sum(1 for c in companies if c["_list"] in discover.DAILY_FILES)
    if daily and n_daily:
        log(f"Daily scan: including {n_daily} companies from your network and the open data")

    max_age = days if days is not None else float(cfg.get("max_age_days", 2))
    boards = cfg.get("boards") or {}
    now = datetime.now(timezone.utc)

    # LinkedIn/Indeed/Naukri (JobSpy) block you when asked too often: at most every N hours.
    skip = []
    js = boards.get("jobspy") or {}
    india_lane(boards)
    js_label = "LinkedIn/Indeed" if "indeed" in (js.get("sites") or []) else "LinkedIn"
    for bname, label in (("jobspy", js_label), ("jobspy_india", "LinkedIn India"),
                         ("jobspy_india_fast", "Naukri/Indeed India")):
        b = boards.get(bname) or {}
        if not (b.get("enabled") and use_boards):
            continue
        gap_h = float(b.get("min_hours_between_runs", 3))
        last = db.get(f"{bname}_last_run")
        if last and not force_linkedin:
            age_h = (now - datetime.fromisoformat(last)).total_seconds() / 3600
            if age_h < gap_h:
                skip.append(bname)
                log(f"{label} skipped this time (last read {age_h:.1f}h ago, limit every {gap_h:g}h)")
        if bname not in skip:
            db.put(f"{bname}_last_run", now.isoformat(timespec="seconds"))
    # Other sites with their own pace (research sites, big feeds): "min_hours_between_runs" in
    # config.yaml. Their ads stay up for weeks, so reading them every 20 minutes gains nothing.
    paced = []
    for bname, b in boards.items():
        if bname.startswith("jobspy") or not (b and b.get("enabled") and use_boards and b.get("min_hours_between_runs")):
            continue
        last = db.get(f"{bname}_last_run")
        if last and (now - datetime.fromisoformat(last)).total_seconds() / 3600 < float(b["min_hours_between_runs"]):
            skip.append(bname)
            paced.append(bname)
        else:
            db.put(f"{bname}_last_run", now.isoformat(timespec="seconds"))
    read_now = [n for n, b in boards.items() if b and b.get("enabled") and b.get("min_hours_between_runs")
                and not n.startswith("jobspy") and n not in paced and use_boards]
    if read_now:
        log("Also reading this time: " + ", ".join(read_now))
    india_on = [b for b in ("jobspy_india", "jobspy_india_fast")
                if (boards.get(b) or {}).get("enabled") and use_boards and b not in skip]
    if any("naukri" in (boards[b].get("sites") or []) for b in india_on):
        from .sources.boards import ensure_jobspy_naukri
        ensure_jobspy_naukri(log)

    # Nothing may fall through the gaps. Normally a job counts if it is at most max_age days old.
    # - a company read for the first time: its jobs of the last first_read_days count (once)
    # - a source the radar couldn't read for a while (PC off, site down, blocked): its jobs since
    #   the last successful read count, so a weekend with the PC off loses nothing
    first_days = float(cfg.get("first_read_days", 30))
    done_ids = set(db.get("backfilled") or [])
    last_ok = db.get("last_ok") or {}
    windows = {}

    def gap_days(key):
        t = last_ok.get(key)
        return None if not t else (now - datetime.fromisoformat(t)).total_seconds() / 86400

    for c in companies:
        cid = company_id(c)
        if cid not in done_ids:
            windows[cid] = (first_days, "older posting: shown because this company was just added to the radar")
        else:
            g = gap_days(cid)
            if g is not None and g + 0.5 > max_age:
                windows[cid] = (min(g + 1, first_days),
                                f"older posting: caught up - this careers page couldn't be read for {g:.0f} days")
    for name in (boards or {}):
        g = gap_days("board:" + name)
        if g is not None and g + 0.5 > max_age:
            windows["board:" + name] = (min(g + 1, first_days),
                                        f"older posting: caught up - {name} couldn't be read for {g:.0f} days")
    board_days = {name: max(max_age, windows.get("board:" + name, (0,))[0]) for name in (boards or {})}
    if boards.get("arbeitsagentur"):
        boards["arbeitsagentur"]["days"] = max(int(boards["arbeitsagentur"].get("days", 2)),
                                               int(-(-board_days["arbeitsagentur"] // 1)))
    for bname in ("jobspy", "jobspy_india", "jobspy_india_fast"):
        if boards.get(bname):
            # the newest only (hours_old), but never less than the time since the last good read
            # (+3 h), so a PC that was off or a blocked read loses nothing
            g = gap_days("board:" + bname)
            need = int(g * 24 + 3) if g is not None else 0
            if "board:" + bname in windows:
                need = max(need, int(board_days[bname] * 24))
            boards[bname]["hours_old"] = max(int(boards[bname].get("hours_old", 24)), min(need, int(first_days * 24)))
    new_ids = {company_id(c) for c in companies} - done_ids
    caught_up = len(windows) - len(new_ids)
    if new_ids:
        log(f"{len(new_ids)} companies read for the first time: including their jobs from the last {first_days:g} days")
    if caught_up:
        log(f"Catching up {caught_up} sources that couldn't be read recently (their older jobs count this time)")

    log(f"Reading {len(companies)} company careers feeds{' + job boards' if use_boards else ''}...")
    jobs, ok, errors, per_source, ok_keys = fetch_all(companies, boards, use_boards, workers, skip, windows)
    ok_ids = {company_id(c) for c in companies} & ok_keys
    first_read_labels = {f"{c['name']} ({c['ats']})" for c in companies if company_id(c) in new_ids}
    total = len(jobs)

    fetch_report = {"fetched": 0, "failed": 0}

    def fetch_missing(cands: list[Job]) -> None:
        for j in cands:                                    # text already read in an earlier scan
            if not j.description:
                j.description = db.known_text(j)
        # Naukri's job pages are built by JavaScript: its skill tags are all there is to read
        cands = [j for j in cands if len(j.description or "") <= 200 and j.source != "naukri"]
        todo = [j for j in cands if db.needs_text(j)]      # new jobs, or ones still unchecked
        if todo:
            log(f"Reading the full description of {len(todo)} jobs that came without one...")
            fetch_report.update(enrich.fetch_descriptions(make_session(), todo, workers))

    unique = dedupe(jobs)
    n_deduped = len(unique)
    for j in unique:                     # text read in an earlier scan: reuse it, don't fetch again
        if not j.description:
            j.description = db.known_text(j)
    from . import semantic
    model = semantic.ACTIVE
    if model is not None:
        model.take_stats()                   # count only this scan
    res = filters.classify(unique, cfg["filters"], max_age, fetch_descriptions=fetch_missing,
                           judge=model.judge if model is not None and model.ready else None)
    model_stats = model.take_stats() if model is not None else {}
    db.add_rejected(res.rejected)
    groups = {"main": res.matched, "maybe": res.maybe,
              "unchecked": [] if cfg["filters"].get("unchecked_jobs", "separate") == "hide" else res.unchecked,
              "low": res.low}
    # research positions (universities, institutes) get their own tab, whatever their score
    groups["research"] = []
    for g in ("main", "unchecked", "low"):
        keep = []
        for j in groups[g]:
            (groups["research"] if is_academic(j) else keep).append(j)
        groups[g] = keep
    # India: its own tab (and low matches with it, marked by their score), Germany stays as it is
    groups["india"], groups["india_low"] = [], []
    for g in ("main", "maybe", "unchecked", "low"):
        keep = []
        for j in groups[g]:
            (groups["india_low" if g == "low" else "india"] if j.india else keep).append(j)
        groups[g] = keep

    home = home_of(cfg)
    net = network.load(root)
    added, new_jobs = {}, []
    for g, lst in groups.items():
        new_jobs += [j for j in lst if db.is_new(j)]
        extra = {j.key: {"km": None if j.india else places.distance_km(j.location, (home["lat"], home["lon"])),
                         "contacts": network.contacts(net, j.company),
                         **({"india": True, "prio": filters.india_priority(j, cfg["filters"])} if j.india else {})}
                 for j in lst}
        added[g] = db.add(lst, g, extra, initial="seen" if g in ("low", "india_low") else "new")   # low match: no "new" ping
    db.prune()
    try:
        yield_rows = keyword_yield(db, jobs, groups, cfg, now.date().isoformat())
    except Exception as e:                     # a statistic must never break a scan
        log(f"Keyword statistics failed: {e}")
        yield_rows = []
    # jobs taken down by the company: feeds that always list all their jobs are compared with what
    # you have open (Workday, Eightfold and plain careers pages list only part, so they're left out)
    listed: dict = {}
    for j in jobs:
        if j.source in FULL_LIST_FEEDS and j.origin and per_source.get(j.origin):
            listed.setdefault(j.origin, set()).add(fingerprint(j.company, j.title))
    n_closed = db.update_listed(listed)
    if n_closed:
        log(f"{n_closed} of your open jobs are no longer listed on the company's careers page (marked closed)")
    # Only now - with every job safely stored - do the sources count as read. If anything above
    # crashed, the next scan still uses the wider window and nothing is lost.
    stamp = now.isoformat(timespec="seconds")
    last_ok.update({k: stamp for k in ok_keys})
    db.put("last_ok", last_ok)
    db.put("backfilled", sorted(done_ids | (new_ids & ok_ids)))
    label_to_id = {f"{c['name']} ({c['ats']})": company_id(c) for c in companies}
    for label, err in errors:
        cid = label_to_id.get(label)
        if cid and re.search(r"\b404\b|not found|not readable", err, re.I):
            e = dead.setdefault(cid, {"n": 0})
            e["n"], e["last"] = e.get("n", 0) + 1, stamp
    for cid in ok_ids:
        dead.pop(cid, None)
    db.put("dead_boards", dead)
    if daily:
        db.put("daily_last_ok", stamp)
    (root / "output").mkdir(exist_ok=True)
    try:
        report.write_rejected(res.rejected, root / "output" / "filtered_out_latest.csv")
    except OSError:          # file open in Excel
        pass

    health = SourceHealth(root / "source_health.json")
    health.record(per_source)
    health.save()
    # only sources still in your lists are reported; boards marked "not found" get their own message
    listed = discover.load_companies(root)
    current_labels = {f"{c['name']} ({c['ats']})" for c in listed} | set(per_source)
    moved_labels = {f"{c['name']} ({c['ats']})" for c in listed if dead.get(company_id(c), {}).get("n", 0) >= 3}
    try:
        all_known = discover.load_companies(root)          # every list, also the daily ones
        # India jobs don't add companies (yet): that would grow the 20-minute company list fast
        auto = discover.discover(make_session(), [j for j in res.matched + res.maybe + res.unchecked if not j.india],
                                 all_known, root, log)
    except Exception as e:                     # never let this break a scan
        log(f"Company discovery failed: {e}")
        auto = []
    sugg = Suggestions(root / "company_suggestions.json", [c["name"] for c in discover.load_companies(root) + auto])
    sugg.add([j for j in new_jobs if not j.india])
    sugg.save()

    from .sources.boards import LAST_JOBSPY_INDIA, INDIA_STATS
    india_read = bool(india_on)
    LAST_JOBSPY_INDIA.clear()
    for b in india_on:                       # only the India lanes read in this scan
        st = INDIA_STATS.get(b) or {}
        LAST_JOBSPY_INDIA.update({k: v for k, v in st.items() if not k.startswith("_")})
        LAST_JOBSPY_INDIA.setdefault("_errors", []).extend(st.get("_errors", []))
    stats = dict(res.stats, fetched=total, deduped=n_deduped, sources_ok=ok, sources_failed=len(errors),
                 passed=len(res.matched), new=added["main"], maybe=added["maybe"], unchecked=added["unchecked"],
                 new_low=added["low"], new_research=added["research"], new_india=added["india"])

    # per-source table for the scan report: jobs read, and how many got through
    through = {}
    for g, lst in groups.items():
        for j in lst:
            k = j.origin or j.source
            through.setdefault(k, {"main": 0, "maybe": 0, "unchecked": 0, "low": 0, "research": 0,
                                   "india": 0, "india_low": 0})[g] += 1
    sources = []
    for label, n in per_source.items():
        t = through.get(label, {})
        sources.append({"source": label, "jobs": n, "main": t.get("main", 0), "maybe": t.get("maybe", 0),
                        "low": t.get("low", 0), "unchecked": t.get("unchecked", 0), "research": t.get("research", 0),
                        "india": t.get("india", 0) + t.get("india_low", 0),
                        "error": next((e for l, e in errors if l == label), ""),
                        "first_read": label in first_read_labels})
    sources.sort(key=lambda x: (x["jobs"] is not None, -(x["main"] + x["maybe"]), -(x["jobs"] or 0)))
    summary = {
        "auto_added": auto,
        "finished": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "seconds": round(time.time() - t0),
        "stats": stats,
        "added": added,
        "linkedin_read": bool(js.get("enabled") and use_boards and "jobspy" not in skip),
        "descriptions": fetch_report,
        "failed": [f"{label}: {err}" for label, err in sorted(errors)],
        "sources": sources,
        "max_age_days": max_age,
        "first_read": len(new_ids), "caught_up": caught_up,
        "daily": daily, "daily_companies": n_daily, "daily_last_ok": db.get("daily_last_ok"),
        "linkedin_skipped": "jobspy" in skip,
        "warnings": health.warnings(current=current_labels, moved=moved_labels)
                    + health_warnings(root, cfg, model, model_stats, fetch_report,
                                      bool(js.get("enabled") and use_boards and "jobspy" not in skip),
                                      india_read),
        "india": {"read": india_read, "per_site": {k: v for k, v in LAST_JOBSPY_INDIA.items() if not k.startswith("_")},
                  "errors": LAST_JOBSPY_INDIA.get("_errors", [])} if india_read else None,
        "never_had_jobs": health.never_had_jobs(current=current_labels),
        "keyword_yield": yield_rows,
        "model": {"status": model.status if model is not None else "off", "ready": bool(model and model.ready),
                  **model_stats},
        "suggestions": [{"name": s["name"], "count": s["count"], "titles": s.get("titles", [])}
                        for s in sugg.top(15)],
    }
    db.put("last_scan", summary)
    db.con.close()
    return summary, res
