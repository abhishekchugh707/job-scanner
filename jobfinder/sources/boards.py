"""German job boards with public APIs (used after the company feeds)."""
from __future__ import annotations

import html
import re
import sys
import time
from dataclasses import replace
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from typing import Optional
from urllib.parse import urljoin

from ..models import Job
from ..util import TIMEOUT, html_to_text, parse_date


# --- Bundesagentur fuer Arbeit (official German job board) ----------------
# Public API the arbeitsagentur.de site itself uses. v4 and v5 stopped working
# in Aug 2026; v6 renamed the response fields.
BA_URL = "https://rest.arbeitsagentur.de/jobboerse/jobsuche-service/pc/v6/jobs"
BA_KEY = "jobboerse-jobsuche"
BA_DETAIL = "https://www.arbeitsagentur.de/jobsuche/jobdetail/"


def arbeitsagentur(session, cfg: dict) -> list[Job]:
    jobs: dict[str, Job] = {}
    errors, answered = [], 0
    for kw in cfg.get("keywords", []):
        try:
            rows = _ba_search(session, cfg, kw)
            answered += 1
        except Exception as e:                       # one failed keyword must not lose the others
            errors.append(f"{kw}: {str(e)[:80]}")
            continue
        for j in rows:
            ref = j.get("referenznummer") or j.get("refnr")
            title = j.get("stellenangebotsTitel") or j.get("titel")
            if ref in jobs and f"Arbeitsagentur: {kw}" not in jobs[ref].found_by:
                jobs[ref].found_by.append(f"Arbeitsagentur: {kw}")
            if not ref or not title or ref in jobs:
                continue
            places = []
            for loc in j.get("stellenlokationen") or []:
                ort = ((loc or {}).get("adresse") or {}).get("ort")
                if ort:
                    places.append(ort)
            if not places and isinstance(j.get("arbeitsort"), dict):
                places.append(j["arbeitsort"].get("ort", ""))
            posted = None
            for k in ("aktuelleVeroeffentlichungsdatum", "veroeffentlichungsdatum",
                      "modifikationsTimestamp", "datumErsteVeroeffentlichung"):
                posted = parse_date(j.get(k))
                if posted:
                    break
            jobs[ref] = Job(
                source="arbeitsagentur",
                company=(j.get("firma") or j.get("arbeitgeber") or "").strip(),
                title=title.strip(),
                url=BA_DETAIL + ref,
                location=" · ".join(dict.fromkeys(places)),
                posted=posted,
                remote=float(j.get("homeofficeprozent") or 0) >= 100,
                country="DE",
                found_by=[f"Arbeitsagentur: {kw}"],
            )
        time.sleep(0.3)                                # be polite between keywords
    if errors and not answered:
        raise RuntimeError("Bundesagentur search failed - " + "; ".join(errors))
    return list(jobs.values())


def _ba_search(session, cfg: dict, keyword: str) -> list[dict]:
    """All result pages for one keyword (100 per page, up to max_pages)."""
    rows: list[dict] = []
    for page in range(1, int(cfg.get("max_pages", 3)) + 1):
        params = {
            "was": keyword,
            "angebotsart": "1",                       # 1 = regular jobs
            "veroeffentlichtseit": str(cfg.get("days", 7)),
            "size": "100",
            "page": str(page),
        }
        if cfg.get("wo"):
            params["wo"] = cfg["wo"]
            params["umkreis"] = str(cfg.get("umkreis", 50))
        r = session.get(BA_URL, params=params, timeout=TIMEOUT,
                        headers={"X-API-Key": BA_KEY, "Accept": "application/json"})
        r.raise_for_status()
        got = r.json().get("ergebnisliste", []) or []
        rows.extend(got)
        if len(got) < 100:
            break
        time.sleep(0.3)
    return rows


# --- Arbeitnow (free API, many English-speaking jobs in Germany) ----------
def arbeitnow(session, cfg: dict) -> list[Job]:
    jobs = []
    url = "https://www.arbeitnow.com/api/job-board-api"
    for _ in range(int(cfg.get("pages", 3))):
        r = session.get(url, timeout=TIMEOUT)
        r.raise_for_status()
        data = r.json()
        for j in data.get("data", []):
            jobs.append(Job(
                source="arbeitnow",
                company=j.get("company_name", "").strip(),
                title=j.get("title", "").strip(),
                url=j.get("url", ""),
                location=j.get("location", ""),
                posted=parse_date(j.get("created_at")),
                description=html_to_text(j.get("description", "")),
                remote=bool(j.get("remote")),
            ))
        url = (data.get("links") or {}).get("next")
        if not url:
            break
    return jobs


# --- small helpers for RSS / XML job feeds ------------------------------------------
_MONTHS = {"jan": 1, "feb": 2, "mar": 3, "mär": 3, "apr": 4, "may": 5, "mai": 5, "jun": 6, "jul": 7,
           "aug": 8, "sep": 9, "oct": 10, "okt": 10, "nov": 11, "dec": 12, "dez": 12}


def any_date(value) -> Optional[datetime]:
    """Dates as feeds write them: RFC 822 ("Fri, 13 Nov 2026 02:00:00 GMT"), 13.11.2026,
    "10. Nov 2026", "2026-11-10 00:00:00" or ISO."""
    s = (value or "").strip()
    if not s:
        return None
    m = re.match(r"(\d{1,2})\.(\d{1,2})\.(\d{4})", s)
    try:
        if m:
            return datetime(int(m[3]), int(m[2]), int(m[1]), tzinfo=timezone.utc)
        m = re.match(r"(\d{1,2})\.?\s+([A-Za-zä]{3})[a-zä]*\.?\s+(\d{4})$", s)
        if m and m[2].lower() in _MONTHS:
            return datetime(int(m[3]), _MONTHS[m[2].lower()], int(m[1]), tzinfo=timezone.utc)
    except ValueError:
        return None
    if re.match(r"\d{4}-\d\d-\d\d \d", s):
        return parse_date(s.replace(" ", "T", 1))
    try:
        dt = parsedate_to_datetime(s)
        return (dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)).astimezone(timezone.utc)
    except (TypeError, ValueError, IndexError):
        return parse_date(s)


def xml_items(xml: str, tag: str = "item") -> list[dict]:
    """The <item> (or <job>) blocks of a feed as {field: [values]} - CDATA unwrapped, entities decoded."""
    out = []
    for raw in re.findall(rf"<{tag}\b[^>]*>(.*?)</{tag}>", xml or "", re.S):
        d: dict = {}
        for name, val in re.findall(r"<([A-Za-z][\w:.-]*)(?:\s[^>]*)?>(.*?)</\1>", raw, re.S):
            cdata = re.match(r"\s*<!\[CDATA\[(.*)\]\]>\s*$", val, re.S)
            d.setdefault(name, []).append((cdata.group(1) if cdata else html.unescape(val)).strip())
        out.append(d)
    return out


def _f(item: dict, name: str) -> str:
    return (item.get(name) or [""])[0]


def _feed(session, url: str, **params) -> str:
    r = session.get(url, params=params or None, timeout=TIMEOUT,
                    headers={"Accept": "application/rss+xml, application/xml, text/xml, text/html;q=0.8, */*;q=0.5"})
    r.raise_for_status()
    return r.text


# --- Berlin Startup Jobs (berlinstartupjobs.com) ----------------------------------------
# Startups post there themselves; few ads, so the last week counts. The feed has only a teaser:
# the description is left empty, so the full ad is read afterwards for the titles that fit.
def berlinstartupjobs(session, cfg: dict) -> list[Job]:
    jobs, errors, answered = {}, [], 0
    days = float(cfg.get("days", 7))
    for feed in cfg.get("feeds", ["engineering"]):
        try:
            items = xml_items(_feed(session, f"https://berlinstartupjobs.com/{feed}/feed/"))
            answered += 1
        except Exception as e:
            errors.append(f"{feed}: {str(e)[:80]}")
            continue
        for it in items:
            title, _, company = _f(it, "title").rpartition(" // ")
            url = _f(it, "link")
            if not (title and company and url):
                continue
            j = Job(source="berlinstartupjobs", company=company.strip(), title=title.strip(), url=url,
                    location="Berlin, Germany", posted=any_date(_f(it, "pubDate")),
                    remote=bool(re.search(r"\bremote\b", title, re.I)))
            j.max_age_days = days
            j.window_reason = "older posting: Berlin Startup Jobs lists few jobs, so its last week counts"
            jobs[url] = j
    if errors and not answered:
        raise RuntimeError("Berlin Startup Jobs not readable - " + "; ".join(errors))
    return list(jobs.values())


# --- GermanTechJobs (germantechjobs.de) ---------------------------------------------------
# Tech jobs posted by the companies, with salary. One XML file with every open ad (~3 MB).
def germantechjobs(session, cfg: dict) -> list[Job]:
    items = xml_items(_feed(session, "https://germantechjobs.de/job_feed.xml"), "job")
    if not items:
        raise RuntimeError("GermanTechJobs: the feed came back without jobs")
    keep_days = float(cfg.get("keep_days", 35))
    now = datetime.now(timezone.utc)
    jobs = []
    for it in items:
        posted = any_date(_f(it, "pubdate"))
        if posted and (now - posted).days > keep_days:
            continue                                  # long-running promoted ads
        title, company, url = _f(it, "title") or _f(it, "name"), _f(it, "company"), _f(it, "link") or _f(it, "url")
        if not (title and company and url):
            continue
        city = _f(it, "city")
        salary = _f(it, "salary")
        desc = html_to_text(_f(it, "description"))
        jobs.append(Job(source="germantechjobs", company=company, title=title, url=url,
                        location=", ".join(x for x in (city, _f(it, "country") or "Germany") if x),
                        posted=posted, description=desc if not salary or salary in desc else f"Salary: {salary}\n{desc}",
                        remote=bool(re.search(r"\bremote\b", title + " " + city, re.I))))
    return jobs



# --- LinkedIn / Indeed via JobSpy ----------------------------------------------
# LinkedIn sends no description in the search; enrich.py reads it afterwards for
# the jobs that pass the filters (fewer requests than fetching every result).
LAST_JOBSPY: dict = {}      # results per site in the last LinkedIn/Indeed read (for the scan report)


def jobspy(session, cfg: dict) -> list[Job]:
    """LinkedIn, Indeed and Google Jobs through python-jobspy.

    results: either one number or per site, e.g. {linkedin: 30, indeed: 100, google: 50}.
    Google Jobs indexes companies' own careers pages (and StepStone, XING ...), so it finds
    jobs at companies that aren't in companies.yaml and aren't on LinkedIn."""
    return _jobspy(cfg, LAST_JOBSPY, [cfg.get("location", "Germany")], cfg.get("country_indeed", "Germany"))


LAST_JOBSPY_INDIA: dict = {}          # this scan's India numbers per site (both India boards together)
INDIA_STATS: dict = {"jobspy_india": {}, "jobspy_india_fast": {}}
INDIA_BOARDS = ("jobspy_india", "jobspy_india_fast")
_NAUKRI_CHECKED: list = []


def jobspy_has_naukri() -> bool:
    """Without importing it: does the installed python-jobspy have the Naukri reader?"""
    import importlib.util
    try:
        spec = importlib.util.find_spec("jobspy")
    except (ImportError, ValueError):
        return False
    from pathlib import Path
    return bool(spec and any((Path(p) / "naukri").is_dir() for p in (spec.submodule_search_locations or [])))


def ensure_jobspy_naukri(log=print) -> bool:
    """Older python-jobspy versions have no Naukri reader: update it once (like the skills model
    installs its packages). Runs before LinkedIn/Indeed are read, so the new version is used."""
    if _NAUKRI_CHECKED:
        return _NAUKRI_CHECKED[0]
    ok = jobspy_has_naukri()
    if not ok and "jobspy" not in sys.modules:
        import subprocess
        exe = sys.executable
        if exe.lower().endswith("pythonw.exe"):
            exe = exe[:-5] + ".exe"
        log("India: updating python-jobspy for Naukri (one time)...")
        for extra in ([], ["--user"]):
            try:
                r = subprocess.run([exe, "-m", "pip", "install", "-U", "-q", "--disable-pip-version-check", *extra,
                                    "python-jobspy"], capture_output=True, text=True, timeout=600,
                                   creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
                if r.returncode == 0:
                    break
            except Exception as e:
                log(f"India: python-jobspy update failed: {e}")
                break
        import importlib
        importlib.invalidate_caches()
        ok = jobspy_has_naukri()
        log("India: Naukri reader ready" if ok else "India: Naukri can't be read (python-jobspy update failed) - LinkedIn/Indeed India still are")
    _NAUKRI_CHECKED.append(ok)
    return ok


def jobspy_india(session, cfg: dict) -> list[Job]:
    """India, slow lane: LinkedIn India (blocks quickly - every 3 hours)."""
    return _india(cfg, "jobspy_india")


def jobspy_india_fast(session, cfg: dict) -> list[Job]:
    """India, fast lane: Naukri and Indeed India (block much less - every hour)."""
    return _india(cfg, "jobspy_india_fast")


def _india(cfg: dict, board: str) -> list[Job]:
    """LinkedIn / Naukri / Indeed India with your keywords. Every job is marked as being in India,
    so it goes to the India tab."""
    cfg = dict(cfg)
    if "naukri" in cfg.get("sites", []) and not jobspy_has_naukri():
        cfg["sites"] = [s for s in cfg["sites"] if s != "naukri"]      # old python-jobspy: the rest still runs
    jobs = _jobspy(cfg, INDIA_STATS[board], cfg.get("locations") or ["India"], "India")
    for j in jobs:
        j.country = "IN"
    return jobs


def _jobspy(cfg: dict, stats: dict, locations: list, country_indeed: str) -> list[Job]:
    try:
        from jobspy import scrape_jobs          # pip install python-jobspy
    except ImportError as e:
        raise RuntimeError("jobspy source is on but python-jobspy is not installed "
                           "(pip install python-jobspy)") from e

    def val(row, key) -> str:
        v = row.get(key)
        if v is None or (isinstance(v, float) and v != v):   # missing or NaN
            return ""
        if isinstance(v, (list, tuple)):
            return ", ".join(str(x) for x in v)
        return str(v).strip()

    sites = cfg.get("sites", ["linkedin", "indeed"])
    res = cfg.get("results", 30)
    wanted = lambda site: int(res.get(site, 30) if isinstance(res, dict) else res)
    hours = int(cfg.get("hours_old", 48))
    jobs, errors, searches = [], [], 0
    stats.clear()
    stats.update({s: 0 for s in sites})
    # broad keywords ("Software Developer") fill LinkedIn's ~30 results with stacks you don't use and
    # LinkedIn blocks first: those run only on the boards that return many results
    broad_sites = [s for s in sites if s not in (cfg.get("broad_skip_sites") or ["linkedin"])]
    plan = [(kw, sites) for kw in cfg.get("keywords", [])] + [(kw, broad_sites) for kw in cfg.get("broad_keywords", [])]
    names = {"linkedin": "LinkedIn", "indeed": "Indeed", "naukri": "Naukri", "google": "Google"}
    region = " India" if country_indeed == "India" else ""
    for kw, kw_sites in plan:
        for location in locations:
            for site in kw_sites:
                searches += 1
                try:
                    extra = {}
                    if site == "google":
                        since = "since yesterday" if hours <= 48 else "in the last week"
                        extra["google_search_term"] = f"{kw} jobs in {location} {since}"
                    df = scrape_jobs(site_name=[site], search_term=kw, location=location,
                                     country_indeed=country_indeed, results_wanted=wanted(site),
                                     hours_old=hours, verbose=0, **extra)
                except Exception as e:                  # one blocked search must not stop the others
                    errors.append(f"{site} '{kw}' {location}: {str(e)[:80]}")
                    continue
                stats[site] = stats.get(site, 0) + len(df)
                for _, row in df.iterrows():
                    posted = parse_date(val(row, "date_posted")[:10] or None)
                    if site == "google" and posted and (datetime.now(timezone.utc) - posted).days > 2:
                        posted = None       # jobspy reads Google's "5 hours ago" as 5 days; the search is "since yesterday"
                    desc = val(row, "description")
                    if not desc and (val(row, "skills") or val(row, "experience_range")):
                        # Naukri's search gives skill tags and experience instead of the ad text
                        desc = "\n".join(x for x in (
                            "Skills: " + val(row, "skills") if val(row, "skills") else "",
                            # "2-5 Yrs" written without the unit, so the "5+ years" seniority rule doesn't misfire
                            "Years of experience: " + re.sub(r"\s*(yrs?|years?)\.?\s*$", "", val(row, "experience_range"), flags=re.I)
                            if val(row, "experience_range") else "") if x)
                    jobs.append(Job(
                        source=val(row, "site") or site,
                        company=val(row, "company"),
                        title=val(row, "title"),
                        url=val(row, "job_url"),
                        location=val(row, "location"),
                        posted=posted,
                        description=desc,
                        remote=val(row, "is_remote").lower() == "true",
                        apply_url=val(row, "job_url_direct"),
                        company_url=val(row, "company_url_direct"),
                        found_by=[f"{names.get(site, site)}{'' if site == 'naukri' else region}: {kw}"],
                    ))
                time.sleep(2)                           # go easy on LinkedIn / Naukri / Google
    if errors:
        stats["_errors"] = errors[:6]
        stats["_searches"] = searches
    if errors and not jobs:
        raise RuntimeError("Job board search failed - " + "; ".join(errors[:4]))
    return jobs


# --- EURAXESS: the EU's research job portal (universities, Max Planck, Fraunhofer, Helmholtz...) --
# --- academics.de (research jobs at German universities and institutes) ----------
# The job board of DIE ZEIT and the Deutscher Hochschulverband: Wissenschaftliche Mitarbeiter,
# research associates and PhD positions. A listing (by subject, newest first) carries a
# schema.org ItemList; each ad page carries a JobPosting (date, employer, place, text).
ACADEMICS = "https://www.academics.de/stellenanzeigen/"
ACADEMICS_LISTS = {          # subject + "Wissenschaftlicher Mitarbeiter" listings
    "informatik": "branche-informatik/position-wissenschaftlicher-mitarbeiter/VTI=",
    "wirtschaftsinformatik": "branche-wirtschaftsinformatik/position-wissenschaftlicher-mitarbeiter/VTM=",
    "mathematik": "branche-mathematik/position-wissenschaftlicher-mitarbeiter/VTU=",
    "elektrotechnik": "branche-elektrotechnik-elektronik/position-wissenschaftlicher-mitarbeiter/VR0=",
}
ACADEMIC_TITLE = re.compile(
    r"wissenschaftlich|research (associate|assistant|scientist|fellow)|researcher|scientist|forscher"
    r"|doktorand|ph\.?\s?d|promotion|predoc|pre-doc|universit\S*assistent|akademische[rn]? (rat|mitarbeiter)",
    re.I)
NOT_FOR_YOU = re.compile(r"professur|professor|studentische|hilfskraft|tutor|lehrkraft|lehrbeauftragt"
                         r"|abschlussarbeit|postdoc|post-doc", re.I)
TOPIC = re.compile(r"machine learning|maschinell\w* lernen|deep learning|\bki\b|künstliche\w* intelligenz"
                   r"|artificial intelligence|\bai\b|data scien|data engineer|data analy|datenanaly|big data"
                   r"|datenwissenschaft|python|software ?(entwicklung|engineering|technik|development)|programmier"
                   r"|\bnlp\b|language model|sprachmodell|\bllm|computer vision|bildverarbeitung|neural|neuronal"
                   r"|reinforcement learning|robotik|robotics", re.I)
_COUNTRY = {"DE": "Germany", "AT": "Austria", "CH": "Switzerland", "NL": "Netherlands", "LU": "Luxembourg"}
_ACADEMICS_SEEN: dict = {}     # ad id -> Job (or None: not for you) - each ad page is read once per run


RESEARCH_EMPLOYER = re.compile(r"universit|hochschule|\bth\b|\btu\b|fachhochschule|institut|fraunhofer|max[- ]planck"
                               r"|helmholtz|leibniz|forschungszentrum|\bdlr\b|\bdfki\b|\bkit\b|klinikum|charit", re.I)
CLEARLY_ACADEMIC = re.compile(r"wissenschaftliche[rn]?\W{0,3}(mitarbeiter|beschäftigte)|research associate|doktorand"
                              r"|ph\.?\s?d\.? (position|student|candidate)|predoc|universit\S*assistent", re.I)


def is_academic(job: Job) -> bool:
    """A research position at a university or institute, whichever site it came from."""
    if job.academic:
        return True
    return academic_title(job.title) and bool(CLEARLY_ACADEMIC.search(job.title) or RESEARCH_EMPLOYER.search(job.company or ""))


def academic_title(title: str) -> bool:
    """A research position (not a professorship, student job, thesis or postdoc)."""
    t = title or ""
    return bool(ACADEMIC_TITLE.search(t)) and not (NOT_FOR_YOU.search(t) and not re.search(r"doktorand|wissenschaftliche[rn]? mitarbeiter", t, re.I))


def _ld(page: str, kind: str) -> list[dict]:
    import json
    out = []
    for m in re.finditer(r'<script[^>]*application/ld\+json[^>]*>(.*?)</script>', page, re.S):
        try:
            d = json.loads(m.group(1))
        except ValueError:
            continue
        out += [o for o in (d if isinstance(d, list) else [d]) if isinstance(o, dict) and o.get("@type") == kind]
    return out


def academics_list(page: str) -> list[tuple[str, str]]:
    """(ad url, title) of a listing page."""
    out = []
    for lst in _ld(page, "ItemList"):
        for el in lst.get("itemListElement") or []:
            it = el.get("item") or {}
            if it.get("@id") and it.get("name"):
                out.append((it["@id"], it["name"].strip()))
    return out


def academics_job(url: str, page: str) -> Optional[Job]:
    jp = next(iter(_ld(page, "JobPosting")), None)
    if not jp:
        return None
    places_ = []
    for loc in jp.get("jobLocation") or []:
        a = (loc or {}).get("address") or {}
        town = a.get("addressLocality") or ""
        country = _COUNTRY.get(a.get("addressCountry") or "", a.get("addressCountry") or "")
        places_.append(", ".join(x for x in (town, country) if x))
    desc = html_to_text(jp.get("description") or "")
    desc = re.sub(r"^Zum Hauptinhalt springen\s*", "", desc)
    return Job(source="academics", company=((jp.get("hiringOrganization") or {}).get("name") or "").strip(),
               title=(jp.get("title") or "").strip(), url=url, location=" · ".join(dict.fromkeys(places_)),
               posted=parse_date(jp.get("datePosted")), description=desc, academic=True)


def academics(session, cfg: dict) -> list[Job]:
    """Research positions (Wissenschaftliche Mitarbeiter, research associates, PhD positions) at
    German universities and institutes, from academics.de, newest first. Only ads whose title is a
    research position are opened, and only once; they must be about computing/data/AI."""
    jobs: dict = {}
    errors, answered = [], 0
    for name in cfg.get("lists", list(ACADEMICS_LISTS)):
        path = ACADEMICS_LISTS.get(name, name)
        for page in range(int(cfg.get("pages", 2))):
            try:
                r = session.get(ACADEMICS + path, params={"sort": "current_desc", **({"page": page + 1} if page else {})},
                                timeout=TIMEOUT, headers={"Accept": "text/html"})
                r.raise_for_status()
                items = academics_list(r.text)
                answered += 1
            except Exception as e:
                errors.append(f"{name}: {str(e)[:80]}")
                break
            fresh = 0
            for url, title in items:
                if url in jobs or not academic_title(title):
                    continue
                if url not in _ACADEMICS_SEEN:
                    fresh += 1
                    try:
                        d = session.get(url, timeout=TIMEOUT, headers={"Accept": "text/html"})
                        d.raise_for_status()
                        j = academics_job(url, d.text)
                    except Exception:
                        continue                    # tried again next scan
                    if j and not TOPIC.search(j.title + " " + j.description):
                        j = None                    # not about computing / data / AI
                    _ACADEMICS_SEEN[url] = j
                    time.sleep(0.5)
                if _ACADEMICS_SEEN[url]:
                    j = replace(_ACADEMICS_SEEN[url], flags=[], matched=[], gaps=[], score=0)
                    j.max_age_days = max(j.max_age_days or 0, float(cfg.get("days", 30)))
                    j.window_reason = "research position - still open, posted within the last weeks"
                    jobs[url] = j
            if len(items) < 20 or (page and not fresh):
                break
            time.sleep(0.5)
    if errors and not answered:
        raise RuntimeError("academics.de not readable - " + "; ".join(errors))
    return list(jobs.values())


# --- more research sources (Research tab) --------------------------------------------------
# All of them post their own ads (no agencies): the federal public-sector job board, the big
# research organisations near Köln, and two university feeds. Same rules as academics.de: the
# title must be a research position and the ad must be about computing / data / AI.
RESEARCH_DAYS = 30


def _as_research(j: Job, cfg: dict) -> Job:
    j.academic = True
    j.max_age_days = max(j.max_age_days or 0, float(cfg.get("days", RESEARCH_DAYS)))
    j.window_reason = "research position - still open, posted within the last weeks"
    return j


def on_topic(title: str, text: str = "") -> bool:
    """About computing / data / AI: the title says so, or the ad names several such topics (one
    passing "data analysis" in a physics or biology ad is not enough)."""
    if TOPIC.search(title or ""):
        return True
    hits = {m.group(0).lower() for m in TOPIC.finditer(text or "")}
    return len(hits) >= (3 if len(text or "") > 1500 else 2)


def _research_fit(title: str, text: str = "") -> bool:
    return academic_title(title) and not re.search(r"post-?do[ck]", title or "", re.I) and on_topic(title, text)


# service.bund.de: every public employer in Germany (universities, Helmholtz centres, federal
# institutes, Interamt). The feed has the newest 500 ads of all kinds; only research titles are opened.
BUND_FEED = "https://www.service.bund.de/Content/Globals/Functions/RSSFeed/RSSGenerator_Stellen.xml"
_BUND_SEEN: dict = {}        # ad url -> Job (or None: not about computing/data/AI) - each ad is opened once


def bund_job(url: str, title: str, feed_desc: str, page: str, posted) -> Job:
    """One ad of service.bund.de. page: its HTML ('' when it couldn't be read - then only the feed's
    employer and place are known)."""
    def strong(label):
        m = re.search(label + r":\s*<strong>(.*?)</strong>", feed_desc or "", re.S)
        return html_to_text(m.group(1)).strip() if m else ""
    employer, place = strong("Arbeitgeber"), re.sub(r"^\d{5}\s+", "", strong("Ort"))
    body = ""
    if page:
        m = re.search(r"Arbeitgeber:\s*([^<]+)</p>", page)
        employer = (m.group(1).strip() if m else "") or employer
        m = re.search(r"<dt>Ort</dt>\s*<dd>\s*([^<]+)", page)
        place = (m.group(1).strip() if m else "") or place
        m = re.search(r"<!--Tätigkeit einfügen-->(.*?)<!--Anforderung einfügen-->", page, re.S) \
            or re.search(r"<!--Tätigkeit einfügen-->(.*?)<!--Tätigkeit ende-->", page, re.S)
        body = html_to_text(m.group(1)).replace("­", "") if m else ""
    return Job(source="bund", company=employer, title=title.replace("­", "").strip(), url=url,
               location=f"{place}, Germany" if place else "Germany", posted=posted, description=body)


def bund(session, cfg: dict) -> list[Job]:
    items = xml_items(_feed(session, BUND_FEED))
    if not items:
        raise RuntimeError("service.bund.de: the feed came back without ads")
    jobs, opened = [], 0
    for it in items:
        title, url = _f(it, "title"), (_f(it, "guid") or _f(it, "link")).split("#")[0]
        if not url or not academic_title(title):
            continue
        if url not in _BUND_SEEN:
            page = ""
            if opened < int(cfg.get("max_open", 60)):
                opened += 1
                try:
                    page = _feed(session, url)
                    time.sleep(0.3)
                except Exception:
                    page = ""
            j = bund_job(url, title, _f(it, "description"), page, any_date(_f(it, "pubDate")))
            if not page:                      # not readable now: judged by the title, tried again next time
                if TOPIC.search(title):
                    jobs.append(_as_research(j, cfg))
                continue
            _BUND_SEEN[url] = j if on_topic(j.title, j.description) else None
        if _BUND_SEEN[url]:
            jobs.append(_as_research(replace(_BUND_SEEN[url], flags=[], matched=[], gaps=[], score=0), cfg))
    return jobs


# Fraunhofer and DLR run the same careers software, with a feed per search word (newest 20 each).
# Their research positions go to the Research tab; their normal engineering jobs are judged like
# any company's jobs (title role words, skills, German check).
def _careers_rss(session, cfg: dict, host: str, company: str, source: str) -> list[Job]:
    jobs, errors, answered = {}, [], 0
    for kw in cfg.get("keywords", ["machine learning", "software"]):
        try:
            items = xml_items(_feed(session, f"https://{host}/services/rss/job/", locale="de_DE", keywords=f"({kw})"))
            answered += 1
        except Exception as e:
            errors.append(f"{kw}: {str(e)[:80]}")
            continue
        for it in items:
            title, url = _f(it, "title"), _f(it, "link") or _f(it, "guid")
            if not (title and url) or url in jobs:
                continue
            place = ""
            m = re.search(r"\s*\(([^()]*)\)\s*$", title)
            if m and not re.search(r"\b[mwfdx]\s*/\s*[mwfdx]\b|gender", m.group(1), re.I):
                title = title[:m.start()].strip()
                place = re.sub(r",?\s*\b\d{5}\b", "", re.sub(r",\s*DE\b", "", m.group(1))).strip(" ,")
            desc = html_to_text(_f(it, "description"))
            j = Job(source=source, company=company, title=title, url=url, posted=any_date(_f(it, "pubDate")),
                    location=f"{place}, Germany" if place else "Germany", description=desc)
            jobs[url] = _as_research(j, cfg) if _research_fit(title, desc) else j
        time.sleep(0.3)
    if errors and not answered:
        raise RuntimeError(f"{company} careers feed not readable - " + "; ".join(errors[:3]))
    return list(jobs.values())


def fraunhofer(session, cfg: dict) -> list[Job]:
    return _careers_rss(session, cfg, "jobs.fraunhofer.de", "Fraunhofer-Gesellschaft", "fraunhofer")


def dlr(session, cfg: dict) -> list[Job]:
    return _careers_rss(session, cfg, "jobs.dlr.de", "DLR (Deutsches Zentrum für Luft- und Raumfahrt)", "dlr")


# DFKI (German Research Center for Artificial Intelligence): every open ad as JSON.
DFKI_FEED = ("https://jobs.dfki.de/utf8/ic_job_feeds.feed_engine?p_web_site_id=6635&p_published_to=WWW"
             "&p_language=DEFAULT&p_direct=Y&p_format=MOBILE&p_include_exclude_from_list=N&p_search=")
_DFKI_SKIP = re.compile(r"ausbildung|praktikum|hilfskraft|diplomand|verwaltung|post-?doc|geschäftsleitung", re.I)


def dfki_jobs(data: dict, cfg: dict) -> list[Job]:
    def cls(job, name):
        for c in (job.get("classifications") or {}).values():
            if (c.get("name") or "").startswith(name):
                return [v.get("class_val", "").strip() for v in c.get("values") or [] if v.get("class_val")]
        return []
    jobs = []
    for d in data.get("jobs") or []:
        area = " ".join(cls(d, "Tätigkeitsbereich"))
        if _DFKI_SKIP.search(area) or not d.get("weblink"):
            continue
        title = (cls(d, "Stellenausschreibung Titel lang - DE") or [d.get("title", "")])[0].replace("\u0096", "-").strip()
        places_ = cls(d, "Standort")
        j = Job(source="dfki", company="DFKI (Deutsches Forschungszentrum für Künstliche Intelligenz)", title=title,
                url=d["weblink"], location=" · ".join(f"{p}, Germany" for p in places_) or "Germany",
                posted=any_date((((d.get("publication") or {}).get("internet") or {}).get("publish_date"))))
        # an AI research centre: every research post there is about computing / AI
        jobs.append(_as_research(j, cfg) if academic_title(title) or re.search(r"wissenschaft|doktorand", area, re.I) else j)
    return jobs


def dfki(session, cfg: dict) -> list[Job]:
    r = session.get(DFKI_FEED, timeout=TIMEOUT, headers={"Accept": "application/json"})
    r.raise_for_status()
    return dfki_jobs(r.json(), cfg)


# TU München: feed of its newest 100 ads for academic staff.
def tum(session, cfg: dict) -> list[Job]:
    items = xml_items(_feed(session, "https://portal.mytum.de/jobs/wissenschaftler/asRss"))
    if not items:
        raise RuntimeError("TU München: the feed came back without ads")
    jobs = []
    for it in items:
        title, desc = _f(it, "title"), html_to_text(_f(it, "description"))
        if not _f(it, "link") or not _research_fit(title, desc):
            continue
        jobs.append(_as_research(Job(source="tum", company="Technische Universität München", title=title,
                                     url=_f(it, "link").replace("http://", "https://"), location="München, Germany",
                                     posted=any_date(_f(it, "pubDate")), description=desc), cfg))
    return jobs


# DAAD PhDGermany: PhD positions at German universities and institutes that are open to
# international applicants (so mostly in English). Feed of the newest ads.
_INSTITUTION = re.compile(
    r"((?:Technische |Technical |Freie |Ruhr[- ]|Humboldt[- ]|Leibniz |Otto[- ]von[- ]Guericke[- ])?Universit(?:ät|y)(?: (?:of|zu|des)\b)?"
    r" [A-ZÄÖÜ][\w.-]+(?: [A-ZÄÖÜ][\w-]+)?|[A-ZÄÖÜ][\w-]+ University(?: of [A-Z]\w+)?|\b(?:TU|RWTH|KIT|LMU|FAU|TUM|TUD) ?[A-ZÄÖÜ]?[\w-]*"
    r"|Max[- ]Planck[- ]Institute?[^.,;(]{0,60}|Helmholtz[- ][^.,;(]{0,60}|Fraunhofer[- ][^.,;(]{0,50}|Leibniz[- ]Institute?[^.,;(]{0,60}"
    r"|Hochschule [A-ZÄÖÜ][\w-]+|[A-ZÄÖÜ][\w-]+ Institute of Technology)")


def daad(session, cfg: dict) -> list[Job]:
    # their server answers 403 to anything that doesn't look like a browser
    r = session.get("https://api.daad.de/api/feeds/rss/en/phd.xml", timeout=TIMEOUT, headers={
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) "
                      "Chrome/130.0.0.0 Safari/537.36",
        "Accept": "application/rss+xml,application/xml;q=0.9,text/html;q=0.8,*/*;q=0.7",
        "Accept-Language": "en-US,en;q=0.9,de;q=0.8",
        "Referer": "https://www2.daad.de/deutschland/promotion/phd/en/13306-phdgermany-database/"})
    r.raise_for_status()
    items = xml_items(r.text)
    if not items:
        raise RuntimeError("DAAD PhDGermany: the feed came back without ads")
    jobs = []
    for it in items:
        title, desc = _f(it, "title"), html_to_text(_f(it, "description"))
        if not _f(it, "link") or not on_topic(title, desc) or re.search(r"post-?doc|professur", title, re.I):
            continue                               # every ad here is a PhD position: only the topic is checked
        m = _INSTITUTION.search(desc[:1500]) or _INSTITUTION.search(desc)
        company = _f(it, "company") or (re.split(r"\.\s|\s+(?:is|invites|offers|seeks|sucht)\b", m.group(1))[0].strip(" .,") if m
                                        else "University (via DAAD PhDGermany)")
        place = _f(it, "location")
        if not re.search(r"ph\.?\s?d|doktorand|doctoral|promotion|research (associate|assistant)|wissenschaftlich", title, re.I):
            title = f"PhD position: {title}"
        jobs.append(_as_research(Job(source="daad", company=company, title=title, url=_f(it, "link"),
                                     location=f"{place}, Germany" if place else "Germany",
                                     posted=any_date(_f(it, "pubDate")), description=desc), cfg))
    return jobs


# RWTH Aachen: its job portal lists the newest ads with the day they were published.
RWTH_PORTAL = "https://www.rwth-aachen.de/cms/root/wir/Karriere/~buym/RWTH-Jobportal/"


def rwth_rows(page: str) -> list[tuple]:
    """(url, title, published, institute) of the portal's table."""
    out = []
    for m in re.finditer(r'<a href="([^"]*/go/id/\w+/file/V\d+/?)"[^>]*>(.*?)</a>(.*?)</tr>', page, re.S):
        title = re.sub(r"\s*\[V\d+\]\s*$", "", html_to_text(m.group(2)).strip())
        rest = m.group(3)
        day = re.search(r"veröffentlicht am (\d{1,2}\.\d{1,2}\.\d{4})", rest)
        cells = re.findall(r"<td[^>]*>(.*?)</td>", rest, re.S)
        inst = ""
        if cells:
            inst = re.sub(r"\s*\[\d+\]\s*$", "", html_to_text(re.sub(r"<h3.*?</h3>", "", cells[0], flags=re.S)).strip())
        out.append((urljoin(RWTH_PORTAL, html.unescape(m.group(1))), title, any_date(day.group(1)) if day else None, inst))
    return out


def rwth(session, cfg: dict) -> list[Job]:
    rows = rwth_rows(_feed(session, RWTH_PORTAL))
    if not rows:
        raise RuntimeError("RWTH Aachen: no ads found on the job portal page (its layout may have changed)")
    jobs = []
    for url, title, posted, inst in rows:
        if _research_fit(f"{title} {inst}"):
            jobs.append(_as_research(Job(source="rwth", company="RWTH Aachen University", title=title, url=url,
                                         location="Aachen, Germany", posted=posted,
                                         description=f"Institute: {inst}" if inst else ""), cfg))
    return jobs


BOARDS = {
    "academics": academics,
    "arbeitsagentur": arbeitsagentur,
    "arbeitnow": arbeitnow,
    "berlinstartupjobs": berlinstartupjobs,
    "germantechjobs": germantechjobs,
    "bund": bund,
    "fraunhofer": fraunhofer,
    "dlr": dlr,
    "dfki": dfki,
    "tum": tum,
    "daad": daad,
    "rwth": rwth,
    "jobspy": jobspy,
    "jobspy_india": jobspy_india,
    "jobspy_india_fast": jobspy_india_fast,
}
