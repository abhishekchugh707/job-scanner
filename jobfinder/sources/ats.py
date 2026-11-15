"""Company careers pages, read through their hiring platform's public feed.

Each fetcher takes (session, company_name, board_id) and returns a list of Job.
These are the same feeds the careers pages themselves load, so a job shows up
here the moment the company publishes it - no LinkedIn delay.
"""
from __future__ import annotations

import json
import re
import xml.etree.ElementTree as ET
from datetime import datetime, timedelta, timezone
from typing import Callable

import requests

from ..models import Job
from ..util import TIMEOUT, html_to_text, one_line, parse_date


class FeedError(Exception):
    """The company's feed could not be read (wrong id, feed switched off...)."""


def _get_json(session: requests.Session, url: str, **kw):
    r = session.get(url, timeout=TIMEOUT, **kw)
    if r.status_code == 404:
        raise FeedError(f"404 - board id not found ({url})")
    r.raise_for_status()
    return r.json()


# --- Greenhouse ------------------------------------------------------------
def greenhouse(session, company: str, board: str) -> list[Job]:
    data = _get_json(session, f"https://boards-api.greenhouse.io/v1/boards/{board}/jobs",
                     params={"content": "true"})
    jobs = []
    for j in data.get("jobs", []):
        jobs.append(Job(
            source="greenhouse", company=company,
            title=j.get("title", "").strip(),
            url=j.get("absolute_url", ""),
            location=(j.get("location") or {}).get("name", ""),
            posted=parse_date(j.get("first_published") or j.get("updated_at")),
            description=html_to_text(j.get("content", "")),
        ))
    return jobs


# --- Lever -----------------------------------------------------------------
def lever(session, company: str, board: str) -> list[Job]:
    # Lever runs a separate EU instance (jobs.eu.lever.co); try the global one first.
    data = None
    for host in ("api.lever.co", "api.eu.lever.co"):
        try:
            data = _get_json(session, f"https://{host}/v0/postings/{board}", params={"mode": "json"})
        except FeedError:
            continue
        if isinstance(data, list):       # Lever answers {"ok": false} for unknown ids
            break
        data = None
    if data is None:
        raise FeedError(f"Lever id '{board}' not found (checked global and EU Lever)")
    jobs = []
    for j in data:
        cat = j.get("categories") or {}
        locs = cat.get("allLocations") or [cat.get("location", "")]
        jobs.append(Job(
            source="lever", company=company,
            title=j.get("text", "").strip(),
            url=j.get("hostedUrl", ""),
            location=" · ".join(l for l in locs if l),
            posted=parse_date(j.get("createdAt")),
            description=j.get("descriptionPlain", "") + "\n" + "\n".join(
                (l.get("text", "") + "\n" + html_to_text(l.get("content", ""))) for l in j.get("lists") or []),
            remote=(j.get("workplaceType") == "remote"),
            country=(j.get("country") or "").upper(),
        ))
    return jobs


# --- Ashby -----------------------------------------------------------------
def ashby(session, company: str, board: str) -> list[Job]:
    data = _get_json(session, f"https://api.ashbyhq.com/posting-api/job-board/{board}")
    jobs = []
    for j in data.get("jobs", []):
        if j.get("isListed") is False:
            continue
        locs = [j.get("location", "")] + [
            (s or {}).get("location", "") for s in j.get("secondaryLocations") or []]
        country = (((j.get("address") or {}).get("postalAddress") or {})
                   .get("addressCountry", ""))
        jobs.append(Job(
            source="ashby", company=company,
            title=j.get("title", "").strip(),
            url=j.get("jobUrl", ""),
            location=" · ".join(l for l in locs if l),
            posted=parse_date(j.get("publishedAt")),
            description=j.get("descriptionPlain", ""),
            remote=bool(j.get("isRemote")),
            country="DE" if country.lower() in ("germany", "deutschland", "de") else "",
        ))
    return jobs


# --- Personio (very common at German startups / Mittelstand) ---------------
def personio(session, company: str, board: str) -> list[Job]:
    last_err = None
    for tld in ("de", "com"):
        url = f"https://{board}.jobs.personio.{tld}/xml"
        try:
            r = session.get(url, params={"language": "en"}, timeout=TIMEOUT,
                            headers={"Accept": "application/xml, text/xml"})
        except requests.RequestException as e:
            last_err = type(e).__name__
            continue
        if r.status_code == 200 and "<workzag-jobs" in r.text[:500]:
            jobs = _parse_personio(r.text, company, board, tld)
            if any(not j.description for j in jobs):
                # asked for English, an ad that exists only in German comes back without its text:
                # read the feed once more without the language, and take the text from there
                try:
                    r2 = session.get(url, timeout=TIMEOUT, headers={"Accept": "application/xml, text/xml"})
                    if r2.status_code == 200 and "<workzag-jobs" in r2.text[:500]:
                        text = {j.url: j.description for j in _parse_personio(r2.text, company, board, tld)}
                        for j in jobs:
                            j.description = j.description or text.get(j.url, "")
                except requests.RequestException:
                    pass
            if jobs and not _personio_pages_on(session, board, tld):
                # the company switched its Personio job pages off (they 404) and shows the ads only
                # on its own site: link to a search for the ad instead of a dead page
                from urllib.parse import quote_plus
                for j in jobs:
                    j.url = "https://www.google.com/search?q=" + quote_plus(f'{company} "{j.title}" karriere')
                    j.flags.append(f"{company}'s Personio job pages are switched off - the link searches "
                                   f"for this ad on their own careers site")
            return jobs
        last_err = f"HTTP {r.status_code}"
    # Some companies switch the XML feed off. Personio's own job list (search.json) still has the
    # jobs, and failing that the careers page does.
    tried = [f"xml: {last_err}"]
    for tld in ("de", "com"):
        url = f"https://{board}.jobs.personio.{tld}/search.json"
        try:
            r = session.get(url, params={"language": "en"}, timeout=TIMEOUT,
                            headers={"Accept": "application/json"})
        except requests.RequestException as e:
            tried.append(f"search.json .{tld}: {type(e).__name__}")
            continue
        data = None
        if r.status_code == 200:
            try:
                data = r.json()
            except ValueError:
                pass
        if isinstance(data, list):
            return [_personio_from_json(j, company, board, tld) for j in data if isinstance(j, dict) and j.get("id")]
        tried.append(f"search.json .{tld}: HTTP {r.status_code}, {len(r.text or "")} chars")
    for tld in ("de", "com"):
        try:
            r = session.get(f"https://{board}.jobs.personio.{tld}/", params={"language": "en"},
                            timeout=TIMEOUT, headers={"Accept": "text/html"})
        except requests.RequestException as e:
            tried.append(f"page .{tld}: {type(e).__name__}")
            continue
        if r.status_code == 200:
            jobs = _parse_personio_page(r.text, company)
            if jobs or _PERSONIO_NONE.search(r.text):
                return jobs                  # a real list, or the page says there are no openings
        tried.append(f"page .{tld}: HTTP {r.status_code}, {len(r.text or "")} chars, no jobs found on it")
    # an empty or unreadable page is a failure, not "0 jobs", so it shows up in the Scan report
    raise FeedError(f"Personio jobs for '{board}' could not be read ({'; '.join(tried)})")


_PERSONIO_NONE = re.compile(r"no (?:open )?(?:positions|vacancies|jobs|openings)|keine (?:offenen )?(?:stellen|positionen|jobs)"
                            r"|currently no|derzeit keine|aktuell keine", re.I)


def _personio_from_json(j: dict, company: str, board: str, tld: str) -> Job:
    offices = j.get("offices") or [j.get("office") or ""]
    return Job(source="personio", company=company,
               title=(j.get("name") or "").strip()[:150],
               url=f"https://{board}.jobs.personio.{tld}/job/{j['id']}?language=en",
               location=" · ".join(o.strip() for o in offices if o and o.strip()),
               description=html_to_text(j.get("description") or ""))


_PERSONIO_PAGES: dict = {}        # board -> (checked at, pages on?) - checked once a day


def _personio_pages_on(session, board: str, tld: str) -> bool:
    import time as _t
    hit = _PERSONIO_PAGES.get(board)
    if hit and _t.time() - hit[0] < 86400:
        return hit[1]
    try:
        r = session.get(f"https://{board}.jobs.personio.{tld}/", timeout=TIMEOUT, headers={"Accept": "text/html"})
        on = r.status_code != 404
    except requests.RequestException:
        return True                       # unknown: keep the normal link
    _PERSONIO_PAGES[board] = (_t.time(), on)
    return on


_PERSONIO_LINK = re.compile(r'<a\b[^>]*href="(https://[a-z0-9-]+\.jobs\.personio\.(?:de|com)/job/\d+)[^"]*"[^>]*>(.*?)</a>',
                            re.S | re.I)


def _parse_personio_page(page: str, company: str) -> list[Job]:
    jobs: dict[str, Job] = {}
    for url, inner in _PERSONIO_LINK.findall(page):
        t = re.search(r"<h3[^>]*>(.*?)</h3>", inner, re.S)
        title = one_line(t.group(1) if t else inner)
        meta = re.search(r'jb-description[^>]*>(.*)', inner, re.S)
        location = ""
        if meta:   # first meta item is the office
            parts = [one_line(p) for p in re.findall(r'MetaItem[^>]*>(.*?)</div>', meta.group(1), re.S)]
            location = next((p for p in parts if p), "")
        jobs.setdefault(url, Job(source="personio", company=company, title=title[:150],
                                 url=url + "?language=en", location=location))
    return list(jobs.values())



def _parse_personio(xml_text: str, company: str, board: str, tld: str) -> list[Job]:
    root = ET.fromstring(xml_text)
    jobs = []
    for p in root.findall("position"):
        pid = (p.findtext("id") or "").strip()
        offices = [p.findtext("office") or ""] + [
            o.text or "" for o in p.findall("additionalOffices/office")]
        desc = "\n".join(
            html_to_text(d.findtext("value") or "") for d in p.findall("jobDescriptions/jobDescription"))
        jobs.append(Job(
            source="personio", company=company,
            title=(p.findtext("name") or "").strip(),
            url=f"https://{board}.jobs.personio.{tld}/job/{pid}?language=en",
            location=" · ".join(o.strip() for o in offices if o and o.strip()),
            posted=parse_date(p.findtext("createdAt")),
            description=desc,
        ))
    return jobs


# --- SmartRecruiters (large companies: Bosch, etc.) -------------------------
def smartrecruiters(session, company: str, board: str) -> list[Job]:
    jobs, offset = [], 0
    while offset < 1000:                 # safety cap
        data = _get_json(session, f"https://api.smartrecruiters.com/v1/companies/{board}/postings",
                         params={"country": "de", "limit": 100, "offset": offset})
        content = data.get("content", [])
        for j in content:
            loc = j.get("location") or {}
            jobs.append(Job(
                source="smartrecruiters", company=company,
                title=j.get("name", "").strip(),
                url=f"https://jobs.smartrecruiters.com/{board}/{j.get('id')}",
                detail_url=f"https://api.smartrecruiters.com/v1/companies/{board}/postings/{j.get('id')}",
                location=loc.get("fullLocation") or loc.get("city", ""),
                posted=parse_date(j.get("releasedDate")),
                remote=bool(loc.get("remote")),
                country=(loc.get("country") or "").upper(),
            ))
        offset += len(content)
        if not content or offset >= data.get("totalFound", 0):
            break
    return jobs


# --- Recruitee ---------------------------------------------------------------
def recruitee(session, company: str, board: str) -> list[Job]:
    # board is either the subdomain ("epilot") or a custom careers domain ("meet.zoi.tech")
    host = board if "." in board else f"{board}.recruitee.com"
    data = _get_json(session, f"https://{host}/api/offers/")
    jobs = []
    for j in data.get("offers", []):
        jobs.append(Job(
            source="recruitee", company=company,
            title=j.get("title", "").strip(),
            url=j.get("careers_url", ""),
            location=j.get("location") or j.get("city", ""),
            posted=parse_date(j.get("published_at") or j.get("created_at")),
            description=html_to_text((j.get("description") or "") + "<br>" + (j.get("requirements") or "")),
            remote=bool(j.get("remote")),
            country=(j.get("country_code") or "").upper(),
        ))
    return jobs


# --- Workable ----------------------------------------------------------------
def workable(session, company: str, board: str) -> list[Job]:
    data = _get_json(session, f"https://apply.workable.com/api/v1/widget/accounts/{board}",
                     params={"details": "true"})          # include the ad text
    jobs = []
    for j in data.get("jobs", []):
        jobs.append(Job(
            source="workable", company=company,
            title=j.get("title", "").strip(),
            url=j.get("url") or f"https://apply.workable.com/{board}/j/{j.get('shortcode')}/",
            location=", ".join(x for x in (j.get("city"), j.get("country")) if x),
            posted=parse_date(j.get("published_on") or j.get("created_at")),
            description=html_to_text(j.get("description") or ""),
            remote=bool(j.get("telecommuting")),
            country="DE" if (j.get("country") or "").lower() in ("germany", "deutschland") else "",
        ))
    return jobs


# --- JOIN (many small German companies) --------------------------------------
# JOIN has no simple public JSON list, but the company page carries its jobs:
# first in the embedded Next.js data (titles + dates), else as plain links.
_JOIN_LINK = re.compile(r'https://join\.com/companies/([a-z0-9._-]+)/(\d+)-([a-z0-9-]+)', re.I)


def join(session, company: str, board: str) -> list[Job]:
    url = f"https://join.com/companies/{board}"
    r = session.get(url, timeout=TIMEOUT, headers={"Accept": "text/html"})
    if r.status_code == 404:
        raise FeedError(f"JOIN page not found ({url})")
    r.raise_for_status()
    page = r.text
    jobs: dict[str, Job] = {}

    m = re.search(r'<script id="__NEXT_DATA__"[^>]*>(.*?)</script>', page, re.S)
    if m:
        try:
            for obj in _walk(json.loads(m.group(1))):
                id_param = obj.get("idParam")
                title = obj.get("title")
                if not (isinstance(id_param, str) and isinstance(title, str) and re.match(r"\d+-", id_param)):
                    continue
                if obj.get("status") not in (None, "ONLINE"):
                    continue
                city = obj.get("city") or {}
                jobs[id_param] = Job(
                    source="join", company=company, title=title.strip(),
                    url=f"{url}/{id_param}",
                    location=(city.get("cityName") or city.get("name") or "") if isinstance(city, dict) else "",
                    posted=parse_date(obj.get("createdAt")),
                    remote=obj.get("workplaceType") == "REMOTE",
                )
        except (ValueError, TypeError):
            pass

    if not jobs:                                    # fallback: job links in the HTML
        for m in _JOIN_LINK.finditer(page):
            slug, num, title_slug = m.groups()
            if slug.lower() != board.lower():
                continue
            key = f"{num}-{title_slug}"
            jobs.setdefault(key, Job(
                source="join", company=company,
                title=title_slug.replace("-", " ").strip().capitalize(),
                url=f"{url}/{key}",
            ))
    return list(jobs.values())


def _walk(node):
    if isinstance(node, dict):
        yield node
        for v in node.values():
            yield from _walk(v)
    elif isinstance(node, list):
        for v in node:
            yield from _walk(v)


# --- Workday (large corporates) ----------------------------------------------
# id = the careers URL, e.g. https://acme.wd3.myworkdayjobs.com/en-US/External
_WD_URL = re.compile(r"https://([\w-]+)\.(wd[\w-]*)\.myworkdayjobs\.com/(?:[a-z]{2}(?:-[A-Z]{2})?/)?([^/?#]+)")
WORKDAY_SEARCHES = ["python", "machine learning", "data engineer", "backend", "software developer"]


def _workday_germany_facet(data: dict):
    """Workday lists its filters ("facets"). Find the one for Germany: a country value named
    Germany/Deutschland, or location values that are in Germany. Returns {param: [ids]} or None."""
    from .. import places
    found: dict[str, list] = {}

    def walk(node, param=None):
        if isinstance(node, list):
            for x in node:
                walk(x, param)
            return
        if not isinstance(node, dict):
            return
        p = node.get("facetParameter") or param
        d = str(node.get("descriptor") or "")
        if node.get("id") and d:
            low = d.lower().strip()
            country_level = "country" in (p or "").lower()
            if low in ("germany", "deutschland") or (
                    not country_level and "location" in (p or "").lower() and places.office_country(d) == "DE"):
                found.setdefault(p, []).append(node["id"])
        walk(node.get("values"), p)

    walk(data.get("facets") or [])
    if not found:
        return None
    # prefer a country-level filter (one id, complete); else all German location ids
    for p in found:
        if "country" in p.lower():
            return {p: found[p]}
    p = max(found, key=lambda k: len(found[k]))
    return {p: found[p]}


def workday(session, company: str, board: str) -> list[Job]:
    """Workday boards: asks for the jobs in Germany only (Workday's own country filter), so big
    companies (Cisco, Accenture...) don't flood the list with Pune or Kraków - and nothing German is
    missed because of a keyword search. Boards without such a filter: keyword searches as before;
    "3 Locations" jobs get their real locations when the detail is read."""
    m = _WD_URL.match(board)
    if not m:
        raise FeedError(f"not a Workday careers URL: {board}")
    tenant, inst, site = m.groups()
    origin = f"https://{tenant}.{inst}.myworkdayjobs.com"
    api = f"{origin}/wday/cxs/{tenant}/{site}/jobs"
    headers = {"Content-Type": "application/json", "Accept": "application/json",
               "Accept-Language": "en-US,en;q=0.9", "Origin": origin, "Referer": f"{origin}/{site}/"}
    jobs: dict[str, Job] = {}

    def page(facets, term, offset):
        r = session.post(api, json={"appliedFacets": facets, "limit": 20, "offset": offset, "searchText": term},
                         headers=headers, timeout=TIMEOUT)
        if r.status_code in (401, 403, 404, 422):
            raise FeedError(f"Workday board not readable (HTTP {r.status_code})")
        r.raise_for_status()
        return r.json() or {}

    def collect(rows):
        for j in rows:
            path = j.get("externalPath") or ""
            if not path or path in jobs:
                continue
            jobs[path] = Job(
                source="workday", company=company,
                title=(j.get("title") or "").strip(),
                url=f"{origin}/{site}{path}",
                detail_url=f"{origin}/wday/cxs/{tenant}/{site}{path}",
                location=j.get("locationsText") or "",
                posted=_workday_posted(j.get("postedOn") or ""),
            )

    def run(facets, terms, max_pages):
        for term in terms:
            for n in range(max_pages):
                data = page(facets, term, n * 20)
                rows = data.get("jobPostings") or []
                collect(rows)
                if len(rows) < 20 or (n + 1) * 20 >= int(data.get("total") or 0):
                    break

    first = page({}, "", 0)
    germany = _workday_germany_facet(first)
    if germany:
        data = page(germany, "", 0)
        total = int(data.get("total") or 0)
        if total <= 600:                          # every job in Germany, no keyword needed
            collect(data.get("jobPostings") or [])
            run(germany, [""], 30) if total > 20 else None
        else:                                     # huge German board: your roles only
            run(germany, WORKDAY_SEARCHES, 5)
        for j in jobs.values():
            if re.match(r"^\d+\s+(locations|standorte)", j.location, re.I) or not j.location:
                j.location = (j.location + " (Germany)").strip()     # filtered to Germany by Workday
        return list(jobs.values())
    run({}, WORKDAY_SEARCHES, 5)
    for j in jobs.values():
        # no Germany filter on this board: a place name the scanner doesn't know ("EMA HQ",
        # "Paya Lebar Quarter") must not count as Germany
        j.strict_location = True
    return list(jobs.values())


def _workday_posted(label: str):
    """'Posted Today' / 'Posted Yesterday' / 'Posted 5 Days Ago' / 'Posted 30+ Days Ago'
    (also the German labels). '30+' means old: it is dated 31 days back so the age
    filter drops it instead of treating it as 'date unknown'."""
    now = datetime.now(timezone.utc)
    low = label.lower()
    if "today" in low or "heute" in low:
        return now
    if "yesterday" in low or "gestern" in low:
        return now - timedelta(days=1)
    if re.search(r"30\s*\+|mehr als 30", low):
        return now - timedelta(days=31)
    m = re.search(r"(\d+)\s*(?:day|tag)", low)
    if m:
        return now - timedelta(days=int(m.group(1)))
    return None


# --- Plain careers page (no hiring platform) ---------------------------------
# id = the careers page URL. Every link on that page that points one level
# below it (e.g. /karriere/<job>) counts as a job; the link text is the title.
# No posting dates, so "new" simply means "not shown before".
_A_TAG = re.compile(r'<a\b[^>]*href="([^"#]+)"[^>]*>(.*?)</a>', re.S | re.I)


_HEADING = re.compile(r"<h[1-6][^>]*>(.*?)</h[1-6]>|<(span|div|p|strong)\b[^>]*class=\"[^\"]*title[^\"]*\"[^>]*>(.*?)</\2>",
                      re.S | re.I)
_LINK_FILLER = re.compile(r"\b(mehr erfahren|jetzt bewerben|zur stelle|details ansehen|learn more|read more|"
                          r"apply now|view job|see details|mehr lesen)\b", re.I)


def _card_title(inner: str) -> str:
    """A link that wraps a whole job card: its heading is the title; drop button words."""
    for h in _HEADING.finditer(inner):
        t = one_line(h.group(1) or h.group(3) or "")
        if t:
            return t
    return " ".join(_LINK_FILLER.sub(" ", one_line(inner)).split())


def page(session, company: str, board: str) -> list[Job]:
    from urllib.parse import urljoin, urlparse
    r = session.get(board, timeout=TIMEOUT, headers={"Accept": "text/html"})
    r.raise_for_status()
    base = urlparse(r.url or board)
    prefix = base.path.rstrip("/") + "/"
    jobs: dict[str, Job] = {}
    for href, inner in _A_TAG.findall(r.text):
        full = urljoin(r.url or board, href)
        p = urlparse(full)
        if p.netloc != base.netloc or not p.path.startswith(prefix):
            continue
        rest = p.path[len(prefix):].strip("/")
        if not rest or "/" in rest:
            continue
        title = _card_title(inner) or rest.replace("-", " ").capitalize()
        jobs.setdefault(full.split("?")[0], Job(source="page", company=company, title=title[:150],
                                                url=full.split("?")[0]))
    return list(jobs.values())


# --- Eightfold (Ericsson, Vodafone, many large companies: jobs.<company>.com/careers) -------
def eightfold(session, company: str, board: str) -> list[Job]:
    """board: the careers host, e.g. "jobs.ericsson.com", optionally "host|domain" when the
    domain isn't the host without its first part. Asks the public search for jobs in Germany,
    newest first (10 per page)."""
    host, _, domain = board.partition("|")
    host = host.replace("https://", "").strip("/")
    domain = domain or host.split(".", 1)[1]
    jobs: dict = {}
    import time as _time
    for start in range(0, 300, 10):               # newest first: the 300 newest German jobs
        if start:
            _time.sleep(0.4)                      # paging too fast gets "too many requests"
        data = _get_json(session, f"https://{host}/api/pcsx/search",
                         params={"domain": domain, "query": "", "location": "Germany", "start": start,
                                 "sort_by": "timestamp"})
        rows = ((data or {}).get("data") or {}).get("positions") or []
        for p in rows:
            pid = p.get("id")
            if not pid or pid in jobs:
                continue
            locs = p.get("locations") or p.get("standardizedLocations") or []
            jobs[pid] = Job(
                source="eightfold", company=company, title=(p.get("name") or "").strip(),
                url=f"https://{host}{p.get('positionUrl') or f'/careers/job/{pid}'}",
                detail_url=f"https://{host}/api/pcsx/position_details?position_id={pid}&domain={domain}&hl=en",
                location=" · ".join(str(x).replace(",", ", ") for x in locs),
                posted=parse_date(p.get("postedTs") or p.get("creationTs")),
                remote=(p.get("workLocationOption") or "").lower() == "remote",
            )
        if len(rows) < 10:
            break
    return list(jobs.values())


FETCHERS: dict[str, Callable[..., list[Job]]] = {
    "eightfold": eightfold,
    "page": page,
    "join": join,
    "workday": workday,
    "greenhouse": greenhouse,
    "lever": lever,
    "ashby": ashby,
    "personio": personio,
    "smartrecruiters": smartrecruiters,
    "recruitee": recruitee,
    "workable": workable,
}
