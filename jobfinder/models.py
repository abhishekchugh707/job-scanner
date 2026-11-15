"""The one data shape every source returns."""
from __future__ import annotations

import hashlib
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Optional


_TRACKING = {"gh_src", "source", "src", "ref", "referrer", "language", "lang", "display",
             "pid", "it", "_ghcid", "lever-source", "lever-origin", "oga", "widgetv2",
             "trk", "trackingid", "refid", "position", "page", "q", "location", "department"}


# Which copy of the same job to keep: the company's own page (0) before a job board that
# companies post on (1-2) before LinkedIn and other aggregators (3).
SOURCE_RANK = {"greenhouse": 0, "lever": 0, "ashby": 0, "personio": 0, "smartrecruiters": 0,
               "recruitee": 0, "workable": 0, "join": 0, "workday": 0, "page": 0, "eightfold": 0,
               "fraunhofer": 0, "dlr": 0, "dfki": 0, "tum": 0, "rwth": 0,
               "arbeitnow": 1, "berlinstartupjobs": 1, "germantechjobs": 1,
               "arbeitsagentur": 2, "bund": 2, "academics": 2, "daad": 2,
               "indeed": 3, "linkedin": 3, "google": 3, "glassdoor": 3, "naukri": 3}


def source_rank(source: str) -> int:
    return SOURCE_RANK.get(source or "", 3)


@dataclass
class Job:
    source: str                 # e.g. "greenhouse", "arbeitsagentur"
    company: str
    title: str
    url: str
    location: str = ""
    posted: Optional[datetime] = None   # timezone-aware UTC when known
    description: str = ""       # plain text, may be empty for some sources
    remote: bool = False
    country: str = ""           # ISO code when the source gives one ("DE")
    detail_url: str = ""        # where the full description can be fetched, if not the job page
    apply_url: str = ""         # the company's own apply link, when a job board gives one
    company_url: str = ""       # the company's own website, when a job board gives one
    strict_location: bool = False   # an unrecognised place doesn't count as Germany (worldwide boards)
    max_age_days: Optional[float] = None   # wider window: company read for the first time, or catch-up
    window_reason: str = ""     # why the wider window (shown on the card)
    origin: str = ""            # which feed it came from, e.g. "N26 (greenhouse)" - for the scan report
    academic: bool = False      # research position (university, institute) - shown in the Research tab
    india: bool = False         # job in India - shown in the India tab
    found_by: list[str] = field(default_factory=list)   # job-board searches that found it ("LinkedIn: Data Engineer")

    # filled in by filters.py
    score: int = 0
    matched: list[str] = field(default_factory=list)
    gaps: list[str] = field(default_factory=list)       # core technologies you don't have
    flags: list[str] = field(default_factory=list)
    german_posting: bool = False

    @property
    def key(self) -> str:
        """Stable id used to remember jobs already shown.

        Tracking parameters (utm_*, gh_src, language, ...) are dropped, but
        parameters that identify the job (gh_jid=123, ashby_jid=..., id=...)
        are kept - many company careers pages put the job id there.
        """
        base, _, query = self.url.partition("?")
        base = base.rstrip("/").lower()
        keep = sorted(p for p in query.split("&") if p and "=" in p
                      and p.split("=")[0].lower() not in _TRACKING
                      and not p.lower().startswith("utm_"))
        ident = base + ("?" + "&".join(keep) if keep else "")
        return hashlib.sha1(ident.encode()).hexdigest()[:16]

    @property
    def age_hours(self) -> Optional[float]:
        if not self.posted:
            return None
        return (datetime.now(timezone.utc) - self.posted).total_seconds() / 3600
