"""Fetch the full job description for jobs whose source only sent a title.

Bundesagentur, LinkedIn, Workday, SmartRecruiters, JOIN, Personio careers pages
and plain careers pages list jobs without their text. Without the text the
German check can't run, so before checking, each such job's own page is read.

Only jobs that already passed the title / location / age checks and that you
haven't seen yet are fetched - usually 20-60 a day.
"""
from __future__ import annotations

import json
import re
import time
from concurrent.futures import ThreadPoolExecutor
from typing import Optional

from .models import Job
from .util import TIMEOUT, html_to_text, parse_date

MAX_FETCH = 250            # safety cap per run


def fetch_descriptions(session, jobs: list[Job], workers: int = 8) -> dict:
    """Fill job.description in place. Returns {'fetched': n, 'failed': n}."""
    jobs = jobs[:MAX_FETCH]
    linkedin = [j for j in jobs if j.source == "linkedin"]
    others = [j for j in jobs if j.source != "linkedin"]
    done = {"fetched": 0, "failed": 0}

    def one(job: Job) -> None:
        try:
            text = _fetch(session, job)
        except Exception:
            text = None
        if text and len(text.strip()) >= 50:
            job.description = text.strip()
            done["fetched"] += 1
        else:
            done["failed"] += 1

    with ThreadPoolExecutor(max_workers=workers) as pool:
        list(pool.map(one, others))
    for j in linkedin:                 # LinkedIn blocks bursts: one at a time
        one(j)
        time.sleep(1.5)
    return done


def _fetch(session, job: Job) -> Optional[str]:
    if job.source == "arbeitsagentur":
        return _arbeitsagentur(session, job)
    if job.source == "workday" and job.detail_url:
        return _workday(session, job)
    if job.source == "eightfold" and job.detail_url:
        d = ((_get(session, job.detail_url, "application/json").json() or {}).get("data") or {})
        return html_to_text(d.get("jobDescription") or "")
    if job.source == "smartrecruiters" and job.detail_url:
        return _smartrecruiters(session, job)
    if job.source == "linkedin":
        return _linkedin(session, job)
    return _generic(session, job.url)


def _get(session, url: str, accept: str = "text/html"):
    r = session.get(url, timeout=TIMEOUT, headers={"Accept": accept, "Accept-Language": "de-DE,de;q=0.9,en;q=0.8"})
    r.raise_for_status()
    return r


# --- Bundesagentur: the page is server-rendered; the ad is in the page's data block
def _arbeitsagentur(session, job: Job) -> Optional[str]:
    page = _get(session, job.url).text
    m = re.search(r'<script id="ng-state" type="application/json">(.*?)</script>', page, re.S)
    if not m:
        return None
    detail = (json.loads(m.group(1)) or {}).get("jobdetail") or {}
    if job.posted is None:
        job.posted = parse_date(detail.get("aktuelleVeroeffentlichungsdatum")
                                or detail.get("datumErsteVeroeffentlichung"))
    return detail.get("stellenangebotsBeschreibung")


# --- Workday: the same job path on the /wday/cxs/ host returns the details as JSON
def _workday(session, job: Job) -> Optional[str]:
    info = (_get(session, job.detail_url, "application/json").json() or {}).get("jobPostingInfo") or {}
    if job.posted is None:
        job.posted = parse_date(info.get("startDate"))
    # "3 Locations" in the list: the detail names them (the location filter runs again afterwards)
    if re.match(r"^\d+\s+(locations|standorte)$", job.location.strip(), re.I):
        locs = [info.get("location")] + list(info.get("additionalLocations") or [])
        locs = [str(x) for x in locs if x]
        if locs:
            job.location = " · ".join(locs)
    return html_to_text(info.get("jobDescription") or "")


# --- SmartRecruiters: public posting API
def _smartrecruiters(session, job: Job) -> Optional[str]:
    data = _get(session, job.detail_url, "application/json").json() or {}
    sections = ((data.get("jobAd") or {}).get("sections") or {})
    parts = [html_to_text((sections.get(k) or {}).get("text") or "")
             for k in ("jobDescription", "qualifications", "additionalInformation", "companyDescription")]
    return "\n".join(p for p in parts if p)


# --- LinkedIn: the public job page carries the description in one block
# (same page and block that JobSpy reads: /jobs/view/<id>, div.show-more-less-html__markup)
def _linkedin(session, job: Job) -> Optional[str]:
    m = re.search(r"/jobs/view/(?:[^/?]*-)?(\d+)", job.url)
    if not m:
        return None
    r = _get(session, f"https://www.linkedin.com/jobs/view/{m.group(1)}")
    if "/jobs/view/" not in (r.url or ""):          # redirected to a login/authwall page
        return None
    return linkedin_description(r.text)


def linkedin_description(page: str) -> Optional[str]:
    start = page.find("show-more-less-html__markup")
    if start < 0:
        return None
    block = page[start:start + 30000]
    block = block[block.find(">") + 1:]
    end = block.find("show-more-less-html__button")   # the "Show more" button follows the text
    return html_to_text(block[:end] if end > 0 else block)


# --- anything else: read the job page and keep its main text
_DROP = re.compile(r"<(script|style|noscript|svg|nav|header|footer|form)\b.*?</\1>", re.S | re.I)


def _generic(session, url: str) -> Optional[str]:
    page = _get(session, url).text
    # JOIN and other Next.js sites: the ad is in the embedded data
    nd = re.search(r'<script id="__NEXT_DATA__"[^>]*>(.*?)</script>', page, re.S)
    if nd:
        texts = [s for s in _strings(json.loads(nd.group(1))) if len(s) > 120]
        if texts:
            return html_to_text("\n".join(texts))
    main = re.search(r"<main\b.*?</main>", page, re.S | re.I)
    return html_to_text(_DROP.sub(" ", main.group(0) if main else page))


def _strings(node):
    if isinstance(node, dict):
        for v in node.values():
            yield from _strings(v)
    elif isinstance(node, list):
        for v in node:
            yield from _strings(v)
    elif isinstance(node, str):
        yield node
