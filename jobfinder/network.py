"""Your LinkedIn network (network/connections.csv, from LinkedIn's data export).

Two uses:
1. contacts(company): who he knows at a company, shown on matching jobs (referrals).
2. probe(): looks for each company's careers feed on the hiring platforms the radar reads
   (Greenhouse, Lever, Ashby, Personio, Recruitee, Workable, SmartRecruiters). Companies
   with a working feed and at least one job in Germany or remote go to companies_network.yaml,
   which every scan reads like companies.yaml. Resumable: progress is kept in
   network_probe.json, so it can stop and continue at any time.
"""
from __future__ import annotations

import csv
import io
import json
import re
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import date
from functools import lru_cache
from pathlib import Path
from typing import Callable, Optional

import yaml

from . import places
from .sources import FETCHERS
from .tracking import norm_company
from .util import TIMEOUT, write_atomic

CSV_PATH = Path("network") / "connections.csv"
OUT_FILE = "companies_network.yaml"
STATE_FILE = "network_probe.json"
CHECK_VERSION = 3          # bump when the board check changes: found boards are checked again

SKIP = re.compile(r"\b(self.?employed|selbstst[äa]ndig|freelanc\w*|freiberuf\w*|student\w*|university|universit[äa]t|"
                  r"hochschule|school|college|institut\w*|stealth|confidential|looking for|open to work|retired|"
                  r"unemployed|career break|various|n/?a)\b", re.I)

# who to show first: people who can refer or hire for tech roles
_RANK = [(re.compile(r"recruit|talent|hr\b|people|human resources|personal", re.I), 0),
         (re.compile(r"engineer|developer|data|ml|machine learning|ai\b|software|tech|cto|devops|platform|"
                     r"architect|entwickler", re.I), 1),
         (re.compile(r"head|lead|director|manager|vp|chief|founder|leiter", re.I), 2)]


def _rank(position: str) -> int:
    for rx, r in _RANK:
        if rx.search(position or ""):
            return r
    return 3


def _read_rows(path: Path) -> list[dict]:
    text = path.read_text(encoding="utf-8-sig", errors="replace")
    i = text.find("First Name")                       # LinkedIn puts a notes block above the header
    return list(csv.DictReader(io.StringIO(text[i:] if i >= 0 else text)))


@lru_cache(maxsize=4)
def _load(path: str, mtime: float) -> dict:
    by_co: dict[str, dict] = {}
    for r in _read_rows(Path(path)):
        co = (r.get("Company") or "").strip()
        key = norm_company(co)
        if not key:
            continue
        e = by_co.setdefault(key, {"name": co, "people": []})
        e["people"].append({"name": f"{(r.get('First Name') or '').strip()} {(r.get('Last Name') or '').strip()}".strip(),
                            "position": (r.get("Position") or "").strip(),
                            "url": (r.get("URL") or "").strip()})
    for e in by_co.values():
        e["people"].sort(key=lambda p: (_rank(p["position"]), p["name"]))
    return by_co


def load(root: Path) -> dict:
    p = root / CSV_PATH
    if not p.exists():
        return {}
    return _load(str(p), p.stat().st_mtime)


_CACHE: dict = {}


def contacts(net: dict, company: str) -> Optional[dict]:
    """People at this company: exact name match, or one name contained in the other."""
    ck = (id(net), company)
    if ck not in _CACHE:
        if len(_CACHE) > 5000:
            _CACHE.clear()
        _CACHE[ck] = _contacts(net, company)
    return _CACHE[ck]


def _contacts(net: dict, company: str) -> Optional[dict]:
    key = norm_company(company or "")
    if not key or not net:
        return None
    e, exact = net.get(key), True
    if e is None:
        exact = False
        for k, v in net.items():
            short = min(k, key, key=len)
            if len(short) >= 5 and short.split()[0] not in AMBIGUOUS and (
                    k.startswith(key + " ") or key.startswith(k + " ")):
                e = v
                break
    if not e:
        return None
    return {"company": e["name"], "total": len(e["people"]), "people": e["people"][:6], "exact": exact}


# first words shared by many unrelated companies: never match on these alone
AMBIGUOUS = {"volksbank", "sparkasse", "deutsche", "stadtwerke", "raiffeisenbank", "sparda", "landesbank",
             "universitat", "universität", "hochschule", "planet", "digital", "global", "smart", "united"}


# ------------------------------------------------------------------ careers-feed probe
GENERIC = {"digital", "global", "smart", "cloud", "future", "united", "first", "green", "media", "group",
           "deutsche", "german", "europe", "european", "international", "software", "consulting", "solutions",
           "systems", "technologies", "services", "data", "creative", "agentur", "studio", "partners"}


def slugs(company: str) -> list[str]:
    base = norm_company(company)
    words = re.sub(r"[^a-z0-9 ]", "", base.replace("ä", "ae").replace("ö", "oe").replace("ü", "ue")
                   .replace("ß", "ss")).split()
    if not words:
        return []
    out = ["".join(words), "-".join(words)]
    if len(words) > 1 and len(words[0]) >= 5 and words[0] not in GENERIC:
        out.append(words[0])
    # boards are often named with the legal form: "ARX Robotics GmbH" -> arxroboticsgmbh
    legal = re.search(r"\b(gmbh|ag|se|ug|kg|inc|ltd|bv)\b", (company or "").lower())
    if legal:
        out.append("".join(words) + legal.group(1))
    elif len(words) > 1:
        out.append("".join(words) + "gmbh")        # German boards are often named like that
    seen, res = set(), []
    for s in out:
        if len(s) >= 4 and s not in seen:
            seen.add(s)
            res.append(s)
    return res[:4]


def _exists(session, ats: str, slug: str) -> bool:
    """Cheap check whether a board exists before reading it fully."""
    urls = {
        "greenhouse": f"https://boards-api.greenhouse.io/v1/boards/{slug}/jobs",
        "lever": f"https://api.lever.co/v0/postings/{slug}?mode=json&limit=1",
        "ashby": f"https://api.ashbyhq.com/posting-api/job-board/{slug}",
        "personio": f"https://{slug}.jobs.personio.de/xml",
        "recruitee": f"https://{slug}.recruitee.com/api/offers/",
        "workable": f"https://apply.workable.com/api/v1/widget/accounts/{slug}",
        "smartrecruiters": f"https://api.smartrecruiters.com/v1/companies/{slug}/postings?limit=1",
    }
    try:
        r = session.get(urls[ats], timeout=TIMEOUT, allow_redirects=False)
    except Exception:
        return False
    if r.status_code != 200:
        return False
    if ats == "smartrecruiters":                         # answers 200 for any name
        try:
            return r.json().get("totalFound", 0) > 0
        except ValueError:
            return False
    if ats == "lever":
        return r.text.strip().startswith("[") and r.text.strip() != "[]"
    if ats == "personio":
        return "<position" in r.text
    if ats == "workable":
        # Workable answers 200 for any account someone once registered: big names are often taken
        # by strangers or left empty. Only an account with job ads counts (an empty one is tried
        # again with the other names after 30 days).
        try:
            return bool(r.json().get("jobs"))
        except (ValueError, AttributeError):
            return False
    return True


PLATFORMS = ["greenhouse", "lever", "ashby", "personio", "recruitee", "workable", "smartrecruiters"]


def _in_germany(job) -> bool:
    """A job in Germany, or remote with Germany/EU named. "Remote" alone doesn't count."""
    loc = job.location or ""
    if job.country == "DE" or re.search(r"germany|deutschland", loc, re.I):
        return True
    if re.search(r"remote|home.?office", loc, re.I) and re.search(r"\b(eu|europe|emea|dach)\b", loc, re.I):
        return True
    return places.country_of(loc) == "DE"


# ---- is the board we found really this company's? ("amazon" on Personio is not Amazon)
_BOILER = re.compile(r"\b(jobs?|at|bei|karriere|career|careers|page|stellenangebote|stellen|offene|open|"
                     r"positions?|job board|work with us|join us|join|team|current|openings|vacancies)\b|[|–—:-]", re.I)
_FILLER_WORDS = GENERIC | {"gmbh", "ag", "se", "kg", "co", "inc", "ltd", "llc", "bv", "the", "and", "und",
                           "deutschland", "germany", "berlin", "munich", "muenchen", "hamburg", "cologne", "koeln"}


def _core(name: str) -> list[str]:
    n = norm_company(_BOILER.sub(" ", name or ""))
    n = n.replace("ä", "ae").replace("ö", "oe").replace("ü", "ue").replace("ß", "ss")
    return [w for w in re.sub(r"[^a-z0-9 ]", " ", n).split() if w not in _FILLER_WORDS]


def _match_one(a: list[str], b: list[str]) -> bool:
    if not a or not b:
        return False
    if a == b or "".join(a) == "".join(b):
        return True
    # "diva-e" vs "diva-e Digital Value Excellence": a two-word name may be the start of the
    # board's full name; a one-word name must match exactly ("Amazon" vs "Amazon Pflege")
    return (len(a) >= 2 and b[:len(a)] == a) or (len(b) >= 2 and a[:len(b)] == b)


def name_matches(company: str, board_name: str) -> bool:
    """Is board_name (as the platform shows it) this company? Page titles like
    "Trackman Careers | Explore Jobs" are checked piece by piece."""
    import html as _html
    shown = _html.unescape(board_name or "")
    a = _core(company)
    parts = [shown] + re.split(r"\s[|–—·-]\s|\s*\|\s*", shown)
    return any(_match_one(a, _core(p)) for p in parts if p.strip())


def _html_title(session, url: str) -> str:
    try:
        r = session.get(url, timeout=TIMEOUT, headers={"Accept": "text/html"})
        if r.status_code != 200:
            return ""
        m = re.search(r"<title[^>]*>(.*?)</title>", r.text or "", re.S | re.I)
        return " ".join(m.group(1).split()) if m else ""
    except Exception:
        return ""


def board_name(session, ats: str, slug: str) -> str:
    """The company name the hiring platform gives for this board ("" if it gives none).
    Taken from the data feeds where possible: page titles are often empty or generic."""
    from collections import Counter
    try:
        if ats == "greenhouse":
            return session.get(f"https://boards-api.greenhouse.io/v1/boards/{slug}", timeout=TIMEOUT).json().get("name", "")
        if ats == "workable":
            return session.get(f"https://apply.workable.com/api/v1/widget/accounts/{slug}",
                               timeout=TIMEOUT).json().get("name", "")
        if ats == "smartrecruiters":
            d = session.get(f"https://api.smartrecruiters.com/v1/companies/{slug}/postings?limit=1",
                            timeout=TIMEOUT).json()
            return ((d.get("content") or [{}])[0].get("company") or {}).get("name", "")
        if ats == "recruitee":
            offers = session.get(f"https://{slug}.recruitee.com/api/offers/", timeout=TIMEOUT).json().get("offers") or []
            names = Counter(o.get("company_name") for o in offers if o.get("company_name"))
            return names.most_common(1)[0][0] if names else ""
        if ats == "personio":
            xml = session.get(f"https://{slug}.jobs.personio.de/xml", timeout=TIMEOUT).text
            names = Counter(" ".join(n.split()) for n in re.findall(r"<subcompany>(.*?)</subcompany>", xml, re.S))
            return names.most_common(1)[0][0] if names else ""
    except Exception:
        return ""
    pages = {"lever": f"https://jobs.lever.co/{slug}", "ashby": f"https://jobs.ashbyhq.com/{slug}"}
    title = _html_title(session, pages[ats]) if ats in pages else ""
    return "" if re.search(r"not found|404|page not found|homepage", title, re.I) else title


def board_is_company(company: str, slug: str, shown: str) -> bool:
    """With a name from the platform: it must match. Without one (Personio feeds without
    subcompany, empty pages): only the board named exactly after the full company name counts
    ("ommax" for OMMAX), never a shortened one ("thomas" for Thomas Henry)."""
    if shown and _core(shown):
        return name_matches(company, shown)
    full = slugs(company)[:2]                    # the full-name forms, not the first word alone
    return slug in full and len(slug) >= 5


_SEMS = {"ashby": threading.Semaphore(3), "workable": threading.Semaphore(2), "smartrecruiters": threading.Semaphore(3),
         "personio": threading.Semaphore(6), "recruitee": threading.Semaphore(4), "greenhouse": threading.Semaphore(8),
         "lever": threading.Semaphore(8)}


def _probe_company(session, name: str) -> Optional[dict]:
    for slug in slugs(name):
        for ats in PLATFORMS:
            with _SEMS[ats]:                      # each platform only a few at a time (no 429 storms)
                found = _exists(session, ats, slug)
            if not found:
                continue
            shown = board_name(session, ats, slug)
            if not board_is_company(name, slug, shown):
                continue                       # someone else's board with the same short name
            try:
                jobs = FETCHERS[ats](session, name, slug)
            except Exception:
                continue
            de = [j for j in jobs if _in_germany(j)]
            return {"name": name, "ats": ats, "id": slug, "board_name": shown, "jobs": len(jobs), "jobs_de": len(de)}
    return None


def _eightfold_host(session, name: str) -> Optional[str]:
    """Eightfold careers sites live on the company's own domain: jobs.<company>.com, careers.<...>"""
    for slug in slugs(name)[:1]:
        for host in (f"jobs.{slug}.com", f"careers.{slug}.com", f"jobs.{slug}.de", f"careers.{slug}.de"):
            try:
                r = session.get(f"https://{host}/api/pcsx/search",
                                params={"domain": host.split(".", 1)[1], "query": "", "location": "Germany",
                                        "start": 0}, timeout=10, allow_redirects=False)
                if r.status_code == 200 and "positions" in r.text:
                    return host
            except Exception:
                continue
    return None


_CAREERS_LINK = re.compile(r'href=["\']([^"\'#]+)["\'][^>]*>([^<]{0,80})', re.I)
_CAREERS_WORD = re.compile(r"karriere|career|jobs?\b|stellen|join[- ]us|work[- ]with[- ]us", re.I)


def careers_from_website(session, url: str, name: str, guess_names: bool = False) -> Optional[dict]:
    """The company's own website -> its careers link -> the hiring platform behind it.
    About 2 requests, and it finds boards whatever they are named."""
    from urllib.parse import urljoin
    from .detect import detect_in_text
    if not url:
        return None
    url = url if url.startswith("http") else "https://" + url
    pages = [url]
    seen = set()
    first = None
    while pages and len(seen) < 4:
        u = pages.pop(0)
        if u in seen:
            continue
        seen.add(u)
        try:
            r = session.get(u, timeout=15, headers={"Accept": "text/html"})
        except Exception:
            continue
        if r.status_code != 200:
            continue
        final = getattr(r, "url", None) or u
        found = detect_in_text(final) or detect_in_text(r.text or "")
        if found and found[0] in FETCHERS and found[0] != "eightfold":
            ats, board = found
            try:
                FETCHERS[ats](session, name, board)
            except Exception:
                continue
            return {"name": name, "ats": ats, "id": board, "via": "website"}
        hidden = _hidden_ashby_board(session, r.text or "", final, name)
        if hidden:
            return {"name": name, "ats": "ashby", "id": hidden, "via": "website"}
        if len(seen) == 1:                         # the home page: follow its careers links
            first = (final, r.text or "")
            for href, text in _CAREERS_LINK.findall(r.text or ""):
                if _CAREERS_WORD.search(href) or _CAREERS_WORD.search(text):
                    pages.append(urljoin(final, href))
    if guess_names and seen:
        return board_from_site_names(session, *(first or (url, "")), name)
    return None


_LINKEDIN_CO = re.compile(r"linkedin\.com/company/([a-z0-9-]{2,60})", re.I)


def board_from_site_names(session, url: str, page: str, name: str) -> Optional[dict]:
    """The job list is built by JavaScript (no platform link in the page): try the names the company
    uses elsewhere - its web address (blp.digital -> blp-digital) and its LinkedIn page - on each
    platform, and keep a board only if the platform shows this company's name for it."""
    from urllib.parse import urlparse
    host = (urlparse(url).hostname or "").lower().removeprefix("www.").removeprefix("careers.").removeprefix("jobs.")
    parts = [p for p in host.split(".") if p]
    li = [m.lower() for m in _LINKEDIN_CO.findall(page or "")]
    li = [m for m in li if m not in ("linkedin",)][:1]
    cands = li + (["-".join(parts), "".join(parts)] if len(parts) > 1 else []) + parts[:1]
    if len(parts) > 2:
        cands += ["-".join(parts[:-1]), "".join(parts[:-1])]
    cands += slugs(name)
    seen = []
    for c in cands:
        if c and len(c) >= 2 and c not in seen:
            seen.append(c)
    names = [name] + [c.replace("-", " ") for c in li] + parts[:1]
    low = (page or "").lower()
    order = sorted(PLATFORMS, key=lambda a: a not in low)   # a platform the page mentions goes first
    for slug in seen[:5]:
        for ats in order:
            with _SEMS[ats]:
                if not _exists(session, ats, slug):
                    continue
            shown = board_name(session, ats, slug)
            if shown and any(name_matches(n, shown) for n in names if n):
                return {"name": name, "ats": ats, "id": slug, "via": "website", "board_name": shown}
            if not shown and li and slug == li[0]:      # same name as its LinkedIn page, no name to compare
                return {"name": name, "ats": ats, "id": slug, "via": "website", "board_name": ""}
    return None


def _hidden_ashby_board(session, page: str, url: str, name: str) -> Optional[str]:
    """Ashby embedded in the company's own site: the page links jobs as ?ashby_jid=<id> but never
    names the board. Try the board names the domain and company name suggest, and keep the one
    that really lists one of those jobs."""
    from urllib.parse import urlparse
    jids = set(re.findall(r"ashby_jid=([0-9a-f-]{36})", page))
    if not jids:
        return None
    host = (urlparse(url).hostname or "").lower().removeprefix("www.").removeprefix("careers.").removeprefix("jobs.")
    parts = host.split(".")
    cands = ["-".join(parts), "".join(parts), parts[0]] if len(parts) > 1 else [parts[0]]
    if len(parts) > 2:                      # blp.digital.de -> drop the country ending
        cands += ["-".join(parts[:-1]), "".join(parts[:-1])]
    cands += slugs(name)
    seen = []
    for c in cands:
        if c and c not in seen:
            seen.append(c)
    for board in seen[:8]:
        try:
            r = session.get(f"https://api.ashbyhq.com/posting-api/job-board/{board}", timeout=TIMEOUT)
            if r.status_code == 200 and any(j in r.text for j in jids):
                return board
        except Exception:
            continue
    return None


def find_careers_page(session, name: str) -> Optional[dict]:
    """A company's careers feed on any platform the radar reads, verified to be that company's."""
    res = _probe_company(session, name)
    if res:
        return res
    host = _eightfold_host(session, name)
    if host:
        return {"name": name, "ats": "eightfold", "id": host, "jobs": 0, "jobs_de": 0}
    return None


class Prober:
    """Background probe, one company at a time per worker; state survives restarts."""

    def __init__(self, root: Path, session_factory: Callable, log: Callable[[str], None] = print, workers: int = 8):
        self.root, self.session_factory, self.log, self.workers = root, session_factory, log, workers
        self.running = False
        self._lock = threading.Lock()
        self.progress = {"done": 0, "total": 0, "found": 0}

    def _state(self) -> dict:
        try:
            return json.loads((self.root / STATE_FILE).read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return {"done": {}}

    def pending(self) -> list[str]:
        net = load(self.root)
        done = self._state()["done"]
        known = {norm_company(c.get("name", "")) for c in _known_companies(self.root)}
        names = [(len(e["people"]), e["name"]) for k, e in net.items()
                 if k not in done and k not in known and not SKIP.search(e["name"]) and slugs(e["name"])]
        return [n for _, n in sorted(names, reverse=True)]       # biggest companies first

    def run(self) -> None:
        if not self._lock.acquire(blocking=False):      # already running
            return
        self.running = True
        try:
            self._run()
        except Exception as e:                    # log it; the next scan starts the check again
            import traceback
            self.log(f"Network check stopped by an error: {e} - it continues after the next scan")
            traceback.print_exc()
        finally:
            self.running = False
            self._lock.release()

    def _run(self) -> None:
        if True:
            self._recheck_added()
            self._add_all_found()
            todo = self.pending()
            state = self._state()
            self.progress = {"done": len(state["done"]), "total": len(state["done"]) + len(todo),
                             "found": sum(1 for v in state["done"].values() if v)}
            if not todo:
                return
            self.log(f"Network: checking careers pages of {len(todo)} companies from the connections list...")
            session = self.session_factory()
            lock = threading.Lock()
            last_save = time.time()
            with ThreadPoolExecutor(max_workers=self.workers) as pool:
                futs = {pool.submit(_probe_company, session, n): n for n in todo}
                for fut in as_completed(futs):
                    name = futs[fut]
                    try:
                        res = fut.result()
                    except Exception:
                        res = None
                    with lock:
                        state["done"][norm_company(name)] = res
                        self.progress["done"] += 1
                        # every verified careers page is read daily - also with no job in Germany
                        # today: the location filter decides job by job, so a new German job isn't missed
                        if res and self._add(res):
                            self.progress["found"] += 1
                            self.log(f"  + {name}: {res['ats']} careers page, {res['jobs_de']} of {res['jobs']} jobs in Germany")
                        if time.time() - last_save > 20:
                            self._save(state)
                            last_save = time.time()
                        if self.progress["done"] % 250 == 0:
                            self.log(f"Network: {self.progress['done']} of {self.progress['total']} companies checked, "
                                     f"{self.progress['found']} careers pages found")
            self._save(state)
            self.log(f"Network: done — {self.progress['found']} careers pages found, all read daily")

    def _recheck_added(self) -> None:
        """Results from an older version of the company check are redone with the current one."""
        state = self._state()
        if state.get("v") == CHECK_VERSION:
            # monthly: companies without a readable careers page are checked again (they may have
            # moved to Greenhouse, Personio...); found boards stay as they are
            started = state.get("pass_started")
            if not started:
                state["pass_started"] = date.today().isoformat()
                self._save(state)
            elif (date.today() - date.fromisoformat(started)).days >= 30:
                state["done"] = {k: v for k, v in state.get("done", {}).items() if v}
                state["pass_started"] = date.today().isoformat()
                self._save(state)
                self.log("Network: monthly re-check of companies without a readable careers page")
            return
        state["done"] = {}         # the old check also wrongly said "no board" for some: redo all
        state["v"] = CHECK_VERSION
        state["pass_started"] = date.today().isoformat()
        self._save(state)
        self._write([])
        self.log("Network: company check improved - re-checking the companies found so far")

    def _add_all_found(self) -> None:
        """Boards found earlier but left out because they had no job in Germany that day: add them."""
        added = 0
        for res in self._state().get("done", {}).values():
            if res and self._add(res):
                added += 1
        if added:
            self.log(f"Network: {added} more careers pages added (no job in Germany yet, but read daily from now on)")

    def _write(self, rows: list) -> None:
        header = ("# Companies from your LinkedIn connections whose careers page the radar\n"
                  "# found and reads every scan. Delete a line to stop reading that company.\n")
        write_atomic(self.root / OUT_FILE, header + yaml.safe_dump(rows, allow_unicode=True, sort_keys=False, width=200))

    def _save(self, state: dict) -> None:
        write_atomic(self.root / STATE_FILE, json.dumps(state))

    def _add(self, res: dict) -> bool:
        p = self.root / OUT_FILE
        old = (yaml.safe_load(p.read_text(encoding="utf-8")) if p.exists() else None) or []
        if any(c["ats"] == res["ats"] and str(c["id"]) == res["id"] for c in old):
            return False                                   # same board under another name
        old.append({"name": res["name"], "ats": res["ats"], "id": res["id"], "board_name": res.get("board_name", ""),
                    "added": date.today().isoformat()})
        self._write(old)
        return True


def _known_companies(root: Path) -> list[dict]:
    out = []
    for name in ("companies.yaml", "companies_auto.yaml"):
        p = root / name
        if p.exists():
            out += yaml.safe_load(p.read_text(encoding="utf-8")) or []
    return out
