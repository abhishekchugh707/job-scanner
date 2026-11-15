"""Offline tests: every source parser, the filters, dedupe and the report.

Run:  python -m pytest -q      (or: python tests/test_jobfinder.py)
"""
import json
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import yaml  # noqa: E402

from jobfinder import filters, report  # noqa: E402
from jobfinder.detect import detect_in_text  # noqa: E402
from jobfinder.models import Job  # noqa: E402
from jobfinder.sources import ats, boards  # noqa: E402
from jobfinder.store import SeenStore  # noqa: E402

NOW = datetime.now(timezone.utc)
ISO = lambda d: (NOW - timedelta(days=d)).isoformat()
MS = lambda d: int((NOW - timedelta(days=d)).timestamp() * 1000)
CFG = yaml.safe_load((ROOT / "config.yaml").read_text(encoding="utf-8"))
CFG["filters"]["india"] = False        # these tests check the Germany rules; India has its own tests (test_india.py)


class Resp:
    def __init__(self, body, status=200):
        self.status_code = status
        self._body = body
        self.text = body if isinstance(body, str) else json.dumps(body)
        self.url = ""

    def json(self):
        return self._body if not isinstance(self._body, str) else json.loads(self._body)

    def raise_for_status(self):
        if self.status_code >= 400:
            raise RuntimeError(f"HTTP {self.status_code}")


class FakeSession:
    def __init__(self, routes):
        self.routes = routes
        self.calls = []

    def get(self, url, params=None, **kw):
        self.calls.append((url, params, kw.get("headers")))
        for frag, resp in self.routes.items():
            if frag in url:
                return resp(params) if callable(resp) else resp
        return Resp("not found", 404)


# ---------------------------------------------------------------- parsers
def test_greenhouse():
    s = FakeSession({"boards-api.greenhouse.io/v1/boards/n26/jobs": Resp({"jobs": [{
        "id": 1, "title": "Backend Engineer (Python)", "absolute_url": "https://job-boards.greenhouse.io/n26/jobs/1",
        "location": {"name": "Berlin, Germany"}, "first_published": ISO(1), "updated_at": ISO(0),
        "content": "&lt;p&gt;We use &lt;b&gt;Python&lt;/b&gt; and FastAPI.&lt;/p&gt;"}]})})
    jobs = ats.greenhouse(s, "N26", "n26")
    assert jobs[0].title == "Backend Engineer (Python)"
    assert jobs[0].location == "Berlin, Germany"
    assert "FastAPI" in jobs[0].description and "<" not in jobs[0].description
    assert s.calls[0][1] == {"content": "true"}
    assert 20 < jobs[0].age_hours < 28


def test_lever_and_unknown_id():
    s = FakeSession({"api.lever.co/v0/postings/quantco-": Resp([{
        "text": "AI Engineer", "hostedUrl": "https://jobs.lever.co/quantco-/abc",
        "categories": {"location": "Cologne", "allLocations": ["Cologne", "Berlin"]},
        "createdAt": MS(2), "descriptionPlain": "Python, MLflow", "workplaceType": "hybrid",
        "country": "DE", "lists": [{"text": "Req", "content": "<li>Docker</li>"}]}]),
        "api.lever.co/v0/postings/nope": Resp({"ok": False, "error": "Document not found"})})
    s.routes["api.eu.lever.co/v0/postings/markt-pilot"] = Resp([{"text": "Platform Engineer",
        "hostedUrl": "https://jobs.eu.lever.co/markt-pilot/x", "categories": {"location": "Stuttgart"}}])
    assert ats.lever(s, "Markt Pilot", "markt-pilot")[0].location == "Stuttgart"   # EU fallback
    j = ats.lever(s, "QuantCo", "quantco-")[0]
    assert j.location == "Cologne · Berlin" and j.country == "DE" and "Docker" in j.description
    try:
        ats.lever(s, "X", "nope")
        assert False
    except ats.FeedError:
        pass


def test_ashby():
    s = FakeSession({"api.ashbyhq.com/posting-api/job-board/deepl": Resp({"jobs": [
        {"title": "ML Engineer", "jobUrl": "https://jobs.ashbyhq.com/deepl/1", "location": "Cologne",
         "secondaryLocations": [{"location": "Remote - Germany"}], "publishedAt": ISO(3),
         "isListed": True, "isRemote": False, "descriptionPlain": "PyTorch",
         "address": {"postalAddress": {"addressCountry": "Germany"}}},
        {"title": "Hidden", "jobUrl": "x", "isListed": False}]})})
    jobs = ats.ashby(s, "DeepL", "deepl")
    assert len(jobs) == 1 and jobs[0].country == "DE" and "Remote - Germany" in jobs[0].location


PERSONIO_XML = """<?xml version="1.0" encoding="UTF-8"?>
<workzag-jobs>
  <position>
    <id>2782451</id><office>Köln</office>
    <additionalOffices><office>Remote</office></additionalOffices>
    <name>(Junior) Backend Engineer (Python) (m/f/d)</name>
    <jobDescriptions><jobDescription><name>Your tasks</name>
      <value><![CDATA[<p>Build FastAPI services on PostgreSQL.</p>]]></value></jobDescription></jobDescriptions>
    <createdAt>%s</createdAt>
  </position>
</workzag-jobs>""" % ISO(1)


def test_personio_falls_back_to_com():
    ats._PERSONIO_PAGES.clear()
    s = FakeSession({"dive.jobs.personio.de/xml": Resp("nope", 404),
                     "dive.jobs.personio.com/xml": Resp(PERSONIO_XML),
                     "dive.jobs.personio.com/": Resp("<html>jobs</html>")})
    j = ats.personio(s, "dive", "dive")[0]
    assert j.url == "https://dive.jobs.personio.com/job/2782451?language=en"
    assert j.location == "Köln · Remote" and "FastAPI" in j.description


def test_personio_german_only_ads_and_switched_off_pages():
    ats._PERSONIO_PAGES.clear()
    pos = lambda text: ("<workzag-jobs><position><id>2876596</id><office>Gescher</office>"
                        "<name>Software Development Engineer (w/m/d)</name><jobDescriptions>" + text +
                        "</jobDescriptions><createdAt>2026-11-05T09:34:06+00:00</createdAt></position></workzag-jobs>")
    german = "<jobDescription><name>Aufgaben</name><value><![CDATA[Backend mit Python und Docker]]></value></jobDescription>"

    def feed(params):                     # English asked: the German-only ad comes without text
        return Resp(pos("") if (params or {}).get("language") == "en" else pos(german))
    s = FakeSession({"d-velop.jobs.personio.de/xml": feed, "d-velop.jobs.personio.de/": Resp("Page Not Found", 404)})
    j = ats.personio(s, "d.velop", "d-velop")[0]
    assert "Backend mit Python" in j.description
    assert j.url.startswith("https://www.google.com/search?q=") and "switched off" in j.flags[0]
    # pages are checked once a day, not every scan
    n = len(s.calls)
    ats.personio(s, "d.velop", "d-velop")
    assert len(s.calls) - n == 2


def test_personio_page_fallback_when_feed_is_off():
    item = ('<a class="job-box" href="https://preisenergie.jobs.personio.com/job/598727?language=en">'
            '<div><h3 class="jb-title">Junior Full Stack Developer, Python + React (m/f/d)</h3>'
            '<div class="page_jobMeta jb-description"><div class="page_jobMetaItem__x"><svg></svg>Munich</div>'
            '<div class="page_jobMetaItem__x">Full-time</div></div></div></a>')
    r = Resp(f"<html><body>{item}</body></html>")
    r.url = "https://preisenergie.jobs.personio.com/?language=en"
    s = FakeSession({"preisenergie.jobs.personio.de/xml": Resp("x", 404),
                     "preisenergie.jobs.personio.com/xml": Resp("x", 404),
                     "preisenergie.jobs.personio.com/": r})
    jobs = ats.personio(s, "preisenergie", "preisenergie")
    assert [(j.title, j.location) for j in jobs] == [("Junior Full Stack Developer, Python + React (m/f/d)", "Munich")]


def test_smartrecruiters_pages():
    def page(params):
        off = params["offset"]
        items = [{"id": str(off + i), "name": f"Software Developer {off + i}", "releasedDate": ISO(1),
                  "location": {"city": "Stuttgart", "country": "de", "fullLocation": "Stuttgart, Germany"}}
                 for i in range(100 if off == 0 else 20)]
        return Resp({"offset": off, "limit": 100, "totalFound": 120, "content": items})
    s = FakeSession({"api.smartrecruiters.com/v1/companies/BoschGroup/postings": page})
    jobs = ats.smartrecruiters(s, "Bosch", "BoschGroup")
    assert len(jobs) == 120 and jobs[0].country == "DE"
    assert jobs[0].url == "https://jobs.smartrecruiters.com/BoschGroup/0"


def test_arbeitsagentur_v6():
    s = FakeSession({"rest.arbeitsagentur.de/jobboerse/jobsuche-service/pc/v6/jobs": Resp({"ergebnisliste": [
        {"referenznummer": "10000-1", "stellenangebotsTitel": "Python Entwickler (m/w/d)",
         "firma": "Muster GmbH", "stellenlokationen": [{"adresse": {"ort": "Köln", "land": "Deutschland"}}],
         "homeofficeprozent": 100},
        {"referenznummer": "10000-1", "stellenangebotsTitel": "dup"}]})})
    jobs = boards.arbeitsagentur(s, {"keywords": ["Python"], "days": 7})
    assert len(jobs) == 1
    j = jobs[0]
    assert j.url == "https://www.arbeitsagentur.de/jobsuche/jobdetail/10000-1"
    assert j.location == "Köln" and j.remote and j.country == "DE"
    url, params, headers = s.calls[0]
    assert headers["X-API-Key"] == "jobboerse-jobsuche" and params["veroeffentlichtseit"] == "7"


def test_arbeitsagentur_pages_and_partial_failure():
    def search(params):
        if params["was"] == "Broken":
            return Resp("error", 500)
        n = 100 if params["page"] == "1" else 7
        return Resp({"ergebnisliste": [{"referenznummer": f"{params['was']}-{params['page']}-{i}",
                                        "stellenangebotsTitel": "Python Entwickler"} for i in range(n)]})
    boards.time.sleep = lambda s: None
    jobs = boards.arbeitsagentur(FakeSession({"pc/v6/jobs": search}), {"keywords": ["Python", "Broken"], "days": 2})
    assert len(jobs) == 107                              # both pages of the working keyword
    try:
        boards.arbeitsagentur(FakeSession({"pc/v6/jobs": search}), {"keywords": ["Broken"]})
        assert False, "all keywords failing must be reported"
    except RuntimeError:
        pass


def test_arbeitnow_pagination():
    s = FakeSession({"job-board-api?page=2": Resp({"data": [], "links": {"next": None}}),
                     "job-board-api": Resp({"data": [{"title": "Data Engineer", "company_name": "Roger",
                                                      "url": "https://www.arbeitnow.com/jobs/x", "location": "Berlin",
                                                      "created_at": int(NOW.timestamp()) - 3600,
                                                      "description": "<p>Airflow</p>", "remote": False}],
                                            "links": {"next": "https://www.arbeitnow.com/api/job-board-api?page=2"}})})
    jobs = boards.arbeitnow(s, {"pages": 5})
    assert len(jobs) == 1 and jobs[0].description == "Airflow" and jobs[0].age_hours < 2


def test_join_next_data_and_link_fallback():
    nd = {"props": {"pageProps": {"jobs": {"items": [
        {"idParam": "16823628-machine-learning-engineer-production-ai", "title": "Machine Learning Engineer (Production AI)",
         "status": "ONLINE", "createdAt": ISO(2), "city": {"cityName": "Munich"}, "workplaceType": "HYBRID"},
        {"idParam": "100-old", "title": "Old", "status": "OFFLINE"}]}}}}
    page = f'<html><script id="__NEXT_DATA__" type="application/json">{json.dumps(nd)}</script></html>'
    s = FakeSession({"join.com/companies/simi": Resp(page)})
    jobs = ats.join(s, "Simi", "simi")
    assert len(jobs) == 1 and jobs[0].title == "Machine Learning Engineer (Production AI)"
    assert jobs[0].location == "Munich" and jobs[0].age_hours < 50
    assert jobs[0].url == "https://join.com/companies/simi/16823628-machine-learning-engineer-production-ai"
    # no embedded data -> read the job links
    links = ('<a href="https://join.com/companies/simi/16823628-machine-learning-engineer-production-ai">x</a>'
             '<a href="https://join.com/companies/other/1-nope">y</a>')
    jobs = ats.join(FakeSession({"join.com/companies/simi": Resp(links)}), "Simi", "simi")
    assert [j.title for j in jobs] == ["Machine learning engineer production ai"]


def test_plain_page_and_recruitee_domain():
    html_page = ('<a href="/karriere/mid-devops-engineer">Mid <b>DevOps</b> Engineer</a>'
                 '<a href="https://www.whitebox.eu/karriere/senior-platform-engineer?x=1">Senior Platform Engineer</a>'
                 '<a href="/karriere#aktuelle-jobs">Jobs</a><a href="/blog/post">Blog</a>'
                 '<a href="https://other.com/karriere/x">x</a>')
    r = Resp(html_page)
    r.url = "https://www.whitebox.eu/karriere"
    jobs = ats.page(FakeSession({"whitebox.eu/karriere": r}), "Whitebox", "https://www.whitebox.eu/karriere")
    assert sorted(j.title for j in jobs) == ["Mid DevOps Engineer", "Senior Platform Engineer"]
    assert "https://www.whitebox.eu/karriere/mid-devops-engineer" in [j.url for j in jobs]
    s = FakeSession({"meet.zoi.tech/api/offers/": Resp({"offers": [
        {"title": "Data Engineer (m/f/d)", "careers_url": "https://meet.zoi.tech/o/data-engineer", "location": "Stuttgart"}]})})
    assert ats.recruitee(s, "Zoi", "meet.zoi.tech")[0].location == "Stuttgart"


class FakePostSession(FakeSession):
    def post(self, url, json=None, **kw):
        self.calls.append((url, json, kw.get("headers")))
        term, off = json["searchText"], json["offset"]
        if term != "python":
            return Resp({"total": 0, "jobPostings": []})
        rows = [{"title": f"Python Developer {off + i}", "externalPath": f"/job/Berlin/Python-Dev_R{off + i}",
                 "locationsText": "Berlin", "postedOn": "Posted 3 Days Ago" if i else "Posted 30+ Days Ago"}
                for i in range(20 if off == 0 else 5)]
        return Resp({"total": 25, "jobPostings": rows})


def test_workday():
    s = FakePostSession({})
    jobs = ats.workday(s, "Acme", "https://acme.wd3.myworkdayjobs.com/en-US/External")
    assert len(jobs) == 25
    assert jobs[1].url == "https://acme.wd3.myworkdayjobs.com/External/job/Berlin/Python-Dev_R1"
    assert jobs[0].age_hours > 30 * 24 and 70 < jobs[1].age_hours < 74    # "30+ days" = old, not unknown
    from jobfinder.sources.ats import _workday_posted
    assert _workday_posted("Vor mehr als 30 Tagen ausgeschrieben").day == (NOW - timedelta(days=31)).day
    assert _workday_posted("Gestern ausgeschrieben") is not None and _workday_posted("") is None
    assert s.calls[0][0] == "https://acme.wd3.myworkdayjobs.com/wday/cxs/acme/External/jobs"


def test_jobspy_handles_missing_values_and_failures():
    import types
    try:
        import pandas as pd
    except ImportError:          # pandas comes with python-jobspy; skip if not installed yet
        return
    fake = types.ModuleType("jobspy")
    calls = []

    def scrape_jobs(**kw):
        calls.append(kw["search_term"])
        if kw["search_term"] == "bad":
            raise RuntimeError("429 blocked")
        return pd.DataFrame([{"site": "linkedin", "title": "ML Engineer", "company": float("nan"),
                              "job_url": "https://www.linkedin.com/jobs/view/1", "location": "Cologne, NRW, DE",
                              "date_posted": "2026-10-28", "description": None, "is_remote": False}])
    fake.scrape_jobs = scrape_jobs
    sys.modules["jobspy"] = fake
    boards.time.sleep = lambda s: None
    try:
        jobs = boards.jobspy(None, {"sites": ["linkedin"], "keywords": ["bad", "ML Engineer"]})
    finally:
        del sys.modules["jobspy"]
    assert calls == ["bad", "ML Engineer"] and len(jobs) == 1
    j = jobs[0]
    assert j.source == "linkedin" and j.company == "" and j.description == "" and j.posted is not None


# ---------------------------------------------------------------- filters
FILLER = (" We are a product team building data-driven software for our customers. You will work"
          " closely with product and design, own features end to end and ship to production every"
          " week. Our working language is English.")


def mk(title, loc="Berlin", desc="", days=1, full=True, **kw):
    """A job with a realistic-length description (full=False: as if the source sent none)."""
    text = (desc + FILLER) if full else desc
    return Job(source="lever", company="Acme", title=title, url=f"https://x/{title}",
               location=loc, posted=NOW - timedelta(days=days), description=text, **kw)


def test_title_rules():
    f = CFG["filters"]
    assert filters.title_ok(mk("Backend Engineer (Python)"), f)
    assert filters.title_ok(mk("Softwareentwickler Backend (m/w/d)"), f)
    assert filters.title_ok(mk("MLOps Engineer"), f)
    assert filters.title_ok(mk("Python-Entwickler"), f)
    assert not filters.title_ok(mk("Senior Backend Engineer"), f)
    assert not filters.title_ok(mk("Sr. Data Engineer"), f)
    assert not filters.title_ok(mk("Werkstudent Python"), f)
    assert not filters.title_ok(mk("Account Executive"), f)
    assert not filters.title_ok(mk("Engineering Manager"), f)


def test_location_rules():
    f = CFG["filters"]
    assert filters.location_ok(mk("x", "Köln"), f)
    assert filters.location_ok(mk("x", "Munich, Bavaria"), f)
    assert filters.location_ok(mk("x", "Remote - Europe"), f)
    assert filters.location_ok(mk("x", "London", country="DE"), f)
    assert not filters.location_ok(mk("x", "London, UK"), f)
    assert not filters.location_ok(mk("x", "Madrid Office"), f)
    j = mk("x", "Remote", remote=True)
    assert filters.location_ok(j, f) and "remote - region not stated" in j.flags
    assert not filters.location_ok(mk("x", "Essex, UK"), f)   # 'essen' must not match inside 'essex'
    # small German towns from today's real results must be kept
    for town in ("Immenstaad am Bodensee", "Mittweida", "Offenbach am Main", "Schwendi, Württemberg",
                 "Tuttlingen", "Hürth"):
        j = mk("x", town)
        assert filters.location_ok(j, f), town
        assert not j.flags, town                      # recognised as German, no warning needed
    for abroad in ("Toulouse, Occitanie", "Toulouse Area"):   # the real 29 Oct misses
        assert not filters.location_ok(mk("x", abroad), f), abroad
    assert not filters.location_ok(mk("x", "Remote", country="FR"), f)
    assert filters.location_ok(mk("x", "Remote - Europe", country="GB", remote=True), f)
    assert filters.location_ok(mk("x", "Tuttlingen, Baden-Württemberg"), f)
    for abroad in ("Barcelona", "Vienna, Austria", "Zürich", "London · Remote", "Bangalore, India",
                   "New York, NY, United States", "Amsterdam"):
        assert not filters.location_ok(mk("x", abroad), f), abroad
    assert filters.location_ok(mk("x", "Berlin · London"), f)        # one of the offices is German


def test_german_language_gate():
    assert filters.check_language(mk("Dev", desc="Fließende Deutschkenntnisse und Englisch.")) == "required"
    assert filters.check_language(mk("Dev", desc="You speak fluent German and English.")) == "required"
    assert filters.check_language(mk("Dev", desc="Sehr gute Deutschkenntnisse (C1) sind erforderlich.")) == "required"
    assert filters.check_language(mk("Dev", desc="Fluent English; German is a plus.")) == "ok"
    assert filters.check_language(mk("Dev", desc="Business fluent German is a plus but not required.")) == "ok"
    j = mk("Dev", desc="Gute Deutschkenntnisse. ")
    assert filters.check_language(j) == "ok" and any(f.startswith("German B2") for f in j.flags)
    assert filters.check_language(mk("Dev", full=False)) == "unchecked"           # no description
    assert filters.check_language(mk("Dev (German speaking)", full=False)) == "required"   # title says it


def test_scoring_prefers_cv_match():
    f = CFG["filters"]
    good, weak = mk("Python Backend Engineer", desc="FastAPI, MLflow, TimescaleDB, Docker"), mk("Software Developer", desc="C# WinForms")
    old = mk("Python Backend Engineer", desc="We need 7+ years of experience. FastAPI", days=10)
    for j in (good, weak, old):
        filters.score(j, f)
    assert good.score > old.score > weak.score
    assert {"python", "fastapi", "mlflow"} <= set(good.matched)


VITRONIC = """**Welche Aufgaben erwarten Dich?**

- Du entwickelst Computer-Vision-Anwendungen in Rust und C++ unter Linux, z. B. mit DNNs, OpenCV etc.
- Du bringst dich aktiv in Forschung & Entwicklung ein, etwa in den Bereichen Adaptivität (Self-Tuning), Edge AI, Clustering und Performance-Optimierung
- Du arbeitest in einem multi-nationalen Team

**Was zeichnet Dich aus?**

- Du verfügst über erste berufliche Erfahrung als Entwickler in Rust oder C/C++, mit Fokus auf Performance
- Du bist sicher im Umgang mit Linux-Anwendungen (insbesondere Ubuntu, CLI, Networking und modernen Workflows inkl. Git)
- Du hast ein grundlegendes Verständnis von Machine-Learning- und Bildverarbeitungs­konzepten, Erfahrungen mit Deep Learning sind von Vorteil
- Du arbeitest gern im Team und verbindest Neugier und Eigeninitiative mit einer pragmatischen Arbeitsweise
- Von Vorteil sind Erfahrungen mit: Python, TypeScript, React, Go, Docker, Postgres, CUDA, KI-Beschleunigern, Tooling, Embedded Systems, SPS/PLCs
"""


def test_scoring_counts_optional_skills_less_and_gaps_against():
    f = CFG["filters"]
    vit = mk("Junior Software Engineer*", desc=VITRONIC)          # the real 29 Oct mismatch
    filters.score(vit, f)
    assert vit.score < f["min_score"], vit.score
    assert {"rust", "c++", "computer vision", "opencv"} <= set(vit.gaps)
    assert "go" not in vit.gaps and "cuda" not in vit.gaps        # only listed as "von Vorteil"
    good = mk("Junior Backend Engineer (Python)", desc="You build FastAPI services on PostgreSQL and run them in Docker.\n"
              "Nice to have: Kubernetes, Rust.")
    filters.score(good, f)
    assert good.score >= 15 and not good.gaps, (good.score, good.gaps)
    # the same skill counts less when it is only optional
    a, b = mk("Dev", desc="Requirements:\n- Python\n"), mk("Dev", desc="Nice to have:\n- Python\n")
    filters.score(a, f); filters.score(b, f)
    assert a.score > b.score


def test_apply_and_freshness():
    jobs = [mk("Python Developer", days=1), mk("Python Developer B", days=30),
            mk("Senior Python Dev"), mk("Python Developer C", loc="Paris")]
    kept, stats = filters.apply(jobs, CFG["filters"], 14)
    assert [j.title for j in kept] == ["Python Developer"]
    assert stats == {"title": 1, "location": 1, "too_old": 1, "german": 0, "company": 0, "low": 0, "negative": 0}


def test_german_compound_titles():
    f = CFG["filters"]
    for t in ("Backendentwickler (m/w/d)", "Webentwickler Python", "Anwendungsentwickler (m/w/d)",
              "Softwareingenieur Cloud", "KI-Entwickler", "Datenbankentwickler"):
        assert filters.title_ok(mk(t), f), t
    for t in ("Produktentwickler Maschinenbau", "Geschäftsentwickler (m/w/d)", "Elektroingenieur"):
        assert not filters.title_ok(mk(t), f), t


def test_no_description_never_reaches_main_list():
    fetched = []

    def fetch(jobs):                      # pretend we could fetch one of the two
        fetched.extend(j.title for j in jobs)
        for j in jobs:
            if j.title == "Python Developer A":
                j.description = "Fließendes Deutsch (C1/C2) ist Pflicht." + FILLER
    jobs = [mk("Python Developer A", full=False), mk("Python Developer B", full=False),
            mk("Python Developer C")]
    res = filters.classify(jobs, CFG["filters"], 2, fetch_descriptions=fetch)
    assert sorted(fetched) == ["Python Developer A", "Python Developer B"]   # only those missing text
    assert [j.title for j in res.matched] == ["Python Developer C"]
    assert [j.title for j in res.unchecked] == ["Python Developer B"]
    assert {j.title for j, _ in res.rejected} == {"Python Developer A"}     # caught after fetching


def test_maybe_section_and_rejected_reasons():
    jobs = [
        mk("Python Developer"),                                                     # match
        mk("Forecasting Engineer", desc="Python, MLflow, FastAPI, TimescaleDB, energy"),  # maybe
        mk("Test Engineer", desc="C++ only"),                                       # rejected: weak
        mk("Account Executive", desc="python fastapi mlflow forecasting energy"),    # not a tech title
        mk("Senior Python Developer"),                                              # excluded word
        mk("Python Developer B", loc="Paris"),                                      # location
        mk("Python Developer C", desc="Fluent German is required. "),               # language
        mk("Python Developer D", days=30),                                          # too old: not logged
    ]
    res = filters.classify(jobs, CFG["filters"], 2)
    # no role word in the title, but the ad asks for your skills: found by skills
    assert [j.title for j in res.matched] == ["Python Developer", "Forecasting Engineer"]
    assert "found by skills" in res.matched[1].flags[0]
    reasons = {j.title: r for j, r in res.rejected}
    assert reasons["Test Engineer"].startswith("title has none of your role words (skill points")
    assert reasons["Account Executive"] == "title contains 'account executive'"
    assert reasons["Senior Python Developer"] == "title contains 'senior'"
    assert reasons["Python Developer B"].startswith("location")
    assert reasons["Python Developer C"].startswith("German required")
    assert "Python Developer D" not in reasons


def test_source_health_warnings():
    import tempfile
    from jobfinder.tracking import SourceHealth
    p = Path(tempfile.mkdtemp()) / "h.json"
    h = SourceHealth(p)
    for counts in ({"A (lever)": 5, "B (ashby)": 3}, {"A (lever)": None, "B (ashby)": 0},
                   {"A (lever)": None, "B (ashby)": 0}, {"A (lever)": None, "B (ashby)": 0}):
        h.record(counts)
    h.save()
    w = SourceHealth(p).warnings()
    assert w == ["A (lever): failed 3 runs in a row",
                 "B (ashby): no open jobs for 3 scans (had some before - probably just not hiring right now)"]
    assert SourceHealth(p).warnings(current={"B (ashby)"}) == w[1:]           # A no longer listed
    assert "moved" in SourceHealth(p).warnings(moved={"A (lever)"})[0]
    h.record({"A (lever)": 4})
    assert not any(x.startswith("A") for x in h.warnings())


def test_company_suggestions():
    import tempfile
    from jobfinder.tracking import Suggestions
    p = Path(tempfile.mkdtemp()) / "s.json"
    s = Suggestions(p, ["N26", "suena.energy", "1KOMMA5°"])
    jobs = [Job(source="arbeitsagentur", company="Rhein Data GmbH", title="Python Entwickler", url="u1"),
            Job(source="linkedin", company="Rhein Data", title="Data Engineer", url="u2"),
            Job(source="linkedin", company="N26 GmbH", title="Backend", url="u3"),        # already known
            Job(source="greenhouse", company="Other AG", title="Backend", url="u4")]      # not a board
    s.add(jobs)
    s.save()
    top = Suggestions(p, ["N26"]).top()
    assert [(x["name"], x["count"]) for x in top] == [("Rhein Data GmbH", 2)]
    assert Suggestions(p, ["N26", "Rhein Data"]).top() == []      # disappears once added


# ------------------------------------------------- dedupe, store, report, detect
def test_dedupe_keeps_company_feed():
    from jobfinder import scan as run
    a = Job(source="arbeitsagentur", company="Acme GmbH", title="Python Developer (m/w/d)", url="https://ba/1")
    b = Job(source="personio", company="Acme GmbH", title="Python Developer", url="https://acme/1")
    assert [j.source for j in run.dedupe([a, b])] == ["personio"]


def test_job_ids_keep_the_job_part_of_the_link():
    a = Job(source="greenhouse", company="trivago", title="A", url="https://careers.trivago.com/apply?gh_jid=4431832002&gh_src=x")
    b = Job(source="greenhouse", company="trivago", title="B", url="https://careers.trivago.com/apply?gh_jid=4431900002&gh_src=x")
    assert a.key != b.key                                   # the bug: these used to collide
    same = Job(source="greenhouse", company="trivago", title="A", url="https://careers.trivago.com/apply?gh_src=y&gh_jid=4431832002&utm_source=li")
    assert a.key == same.key                                # tracking parts still ignored
    # ids for plain links are unchanged, so jobs already seen stay hidden
    import hashlib
    p = Job(source="personio", company="x", title="t", url="https://x.jobs.personio.de/job/598727?language=en")
    assert p.key == hashlib.sha1(b"https://x.jobs.personio.de/job/598727").hexdigest()[:16]


def test_description_readers():
    from jobfinder import enrich
    # Bundesagentur: the ad text is in the page's ng-state data block
    state = {"jobdetail": {"stellenangebotsBeschreibung": "Fließendes Deutsch (C1) und Python." + FILLER,
                           "datumErsteVeroeffentlichung": "2026-10-28"}}
    ba_page = f'<html><script id="ng-state" type="application/json">{json.dumps(state)}</script></html>'
    j = Job(source="arbeitsagentur", company="X", title="Python Entwickler", url="https://www.arbeitsagentur.de/jobsuche/jobdetail/1-S")
    assert "Fließendes Deutsch" in enrich._arbeitsagentur(FakeSession({"jobdetail/1-S": Resp(ba_page)}), j)
    assert j.posted is not None
    # Workday detail JSON
    wd = Resp({"jobPostingInfo": {"jobDescription": "<p>Fluent German required.</p>", "startDate": "2026-10-28"}})
    j = Job(source="workday", company="Z", title="Dev", url="u", detail_url="https://z.wd3.myworkdayjobs.com/wday/cxs/z/S/job/x")
    assert enrich._workday(FakeSession({"wday/cxs/z/S/job/x": wd}), j) == "Fluent German required." and j.posted
    # SmartRecruiters detail JSON
    sr = Resp({"jobAd": {"sections": {"jobDescription": {"text": "<p>Build pipelines</p>"},
                                      "qualifications": {"text": "<li>Very good German</li>"}}}})
    j = Job(source="smartrecruiters", company="F", title="Dev", url="u", detail_url="https://api.smartrecruiters.com/v1/companies/F/postings/1")
    assert "Very good German" in enrich._smartrecruiters(FakeSession({"postings/1": sr}), j)
    # LinkedIn job page block, and redirect to a login wall = no text
    li_page = ('<div class="show-more-less-html__markup show-more-less-html__markup--clamp-after-5">'
               '<p>We need <strong>fluent German</strong>.</p><div><ul><li>Python</li></ul></div></div>'
               '<button class="show-more-less-html__button">Show more</button>')
    ok = Resp(li_page); ok.url = "https://www.linkedin.com/jobs/view/123"
    j = Job(source="linkedin", company="L", title="Dev", url="https://www.linkedin.com/jobs/view/123")
    text = enrich._linkedin(FakeSession({"jobs/view/123": ok}), j)
    assert "fluent German" in text and "Python" in text and "Show more" not in text
    wall = Resp("<html>Sign in</html>"); wall.url = "https://www.linkedin.com/authwall?x"
    assert enrich._linkedin(FakeSession({"jobs/view/123": wall}), j) is None
    # fetch_descriptions fills in place and counts failures instead of raising
    jobs = [Job(source="workday", company="Z", title="A", url="u1", detail_url="https://z.wd3.myworkdayjobs.com/wday/cxs/z/S/job/x"),
            Job(source="workday", company="Z", title="B", url="u2", detail_url="https://z.wd3.myworkdayjobs.com/wday/cxs/z/S/job/missing")]
    wd2 = Resp({"jobPostingInfo": {"jobDescription": "<p>" + "Python backend work. " * 10 + "</p>"}})
    report_ = enrich.fetch_descriptions(FakeSession({"job/x": wd2}), jobs, workers=2)
    assert report_ == {"fetched": 1, "failed": 1} and jobs[0].description and not jobs[1].description


def test_seen_store(tmp_path=None):
    import tempfile
    p = Path(tempfile.mkdtemp()) / "seen.json"
    s = SeenStore(p)
    j = mk("Python Developer")
    assert s.is_new(j)
    s.add(j)
    s.save()
    assert not SeenStore(p).is_new(mk("Python Developer"))


def test_report_files():
    import tempfile
    d = Path(tempfile.mkdtemp())
    j = mk("Python </script> Developer", desc="FastAPI")
    filters.score(j, CFG["filters"])
    report.write_html([j], d / "r.html", {"fetched": 10, "sources_ok": 2, "new": 1})
    report.write_csv([j], d / "r.csv")
    page = (d / "r.html").read_text(encoding="utf-8")
    assert "Python <\\/script> Developer" in page and "</script> Developer" not in page
    assert "10 jobs read from 2 sources" in page
    assert "Python </script> Developer" in (d / "r.csv").read_text(encoding="utf-8-sig")


def test_detect():
    assert detect_in_text("https://jobs.lever.co/quantco-/123") == ("lever", "quantco-")
    assert detect_in_text('<iframe src="https://boards.greenhouse.io/embed/job_board?for=celonis">') == ("greenhouse", "celonis")
    assert detect_in_text("https://job-boards.greenhouse.io/n26/jobs/1") == ("greenhouse", "n26")
    assert detect_in_text("https://flexa.jobs.personio.de/job/1") == ("personio", "flexa")
    assert detect_in_text("https://jobs.ashbyhq.com/black-forest-labs") == ("ashby", "black-forest-labs")
    assert detect_in_text("https://careers.smartrecruiters.com/BoschGroup") == ("smartrecruiters", "BoschGroup")
    assert detect_in_text("https://example.com/karriere") is None
    assert detect_in_text("humaNoo.jobs.personio.de/") == ("personio", "humanoo")
    assert detect_in_text("https://join.com/companies/simi/123-x") == ("join", "simi")
    assert detect_in_text('href="https://acme.wd3.myworkdayjobs.com/en-US/External/job/x"') == \
        ("workday", "https://acme.wd3.myworkdayjobs.com/en-US/External")


def test_yaml_files_valid():
    comps = yaml.safe_load((ROOT / "companies.yaml").read_text(encoding="utf-8"))
    assert len(comps) > 50
    assert all(c["ats"] in ats.FETCHERS and c["id"] and c["name"] for c in comps)
    ids = [(c["ats"], c["id"].lower()) for c in comps]
    assert len(ids) == len(set(ids)), "duplicate company"


if __name__ == "__main__":
    fails = 0
    for name, fn in list(globals().items()):
        if name.startswith("test_"):
            try:
                fn()
                print("ok  ", name)
            except Exception as e:
                fails += 1
                print("FAIL", name, repr(e))
    sys.exit(1 if fails else 0)


def test_company_read_for_the_first_time_shows_older_openings_once():
    f = CFG["filters"]
    old = mk("Python Developer", desc="Python, FastAPI, MLflow, Docker", days=12)
    assert not filters.fresh_ok(old, 2)
    old.max_age_days = 30                      # set by the scan for a newly added company
    res = filters.classify([old], f, 2)
    assert [j.title for j in res.matched] == ["Python Developer"]
    assert any("just added" in fl for fl in res.matched[0].flags)


def test_unchecked_jobs_with_a_foreign_stack_in_the_title_are_dropped():
    f = CFG["filters"]
    weak = mk("Software Engineer (Go)", desc="", full=False)
    plain = mk("Data Engineer (m/w/d)", desc="", full=False)
    res = filters.classify([weak, plain], f, 2)
    assert [j.title for j in res.unchecked] == ["Data Engineer (m/w/d)"]
    assert "stack you don't use" in res.rejected[0][1]


def test_card_links_use_the_title_element():
    inner = ('<div><span class="box__intro">TEAM SOFTWARE ENGINEERING</span>'
             '<span class="box__title">Mid DevOps Engineer (m/f/x)</span><p>Remote</p>'
             '<span class="more">Mehr erfahren</span></div>')
    assert ats._card_title(inner) == "Mid DevOps Engineer (m/f/x)"
    assert ats._card_title("Backend Developer <span>Mehr erfahren</span>") == "Backend Developer"


def test_low_skill_jobs_go_to_their_own_tab_not_the_bin():
    f = CFG["filters"]
    low = mk("Backend Engineer", desc="TypeScript and Node services for our platform.")
    bad = mk("Backend Engineer (Go)", desc="Go and Rust in production.")
    res = filters.classify([low, bad], f, 2)
    assert [j.title for j in res.low] == ["Backend Engineer"]
    assert res.matched == [] and "you don't use" in res.rejected[0][1]


def test_us_towns_with_german_names_are_not_germany():
    f = CFG["filters"]
    for loc in ["Berlin, CT, United States", "Hamburg, NY", "Heidelberg, PA", "Remote - US", "Frankfort, KY",
                "Wilmington, DE", "Pune, Gera Commerzone SEZ", "Krakow, Poland", "US-MD-Hanover", "Peru",
                "ROM - BUCHAREST", "KSA - RYD G", "Latin America"]:
        assert not filters.location_ok(mk("Python Developer", loc=loc), f), loc
    for loc in ["Berlin · London", "Berlin, Germany; Austin, TX", "Wiesbaden, DE", "Köln / Remote", "Hürth",
                "Germany - Hamburg (Atlantic Haus)", "Leonberg, Germany", "Gera", "DE", "Stuttgart, DE"]:
        assert filters.location_ok(mk("Python Developer", loc=loc), f), loc


class FacetSession(FakeSession):
    """A Workday board with a country filter: Germany has 3 jobs, the board 900 worldwide."""
    def post(self, url, json=None, **kw):
        self.calls.append((url, json, None))
        facets = {"facets": [{"facetParameter": "locationMainGroup", "values": [
            {"facetParameter": "locationCountry", "descriptor": "Country", "values": [
                {"descriptor": "India", "id": "in1", "count": 800},
                {"descriptor": "Germany", "id": "de1", "count": 3}]}]}]}
        if json["appliedFacets"] == {"locationCountry": ["de1"]}:
            rows = [{"title": t, "externalPath": f"/job/x/{i}", "locationsText": loc, "postedOn": "Posted Today"}
                    for i, (t, loc) in enumerate([("Python Developer", "Berlin"), ("Data Engineer", "2 Locations"),
                                                  ("ML Engineer", "Munich")])]
            return Resp({"total": 3, "jobPostings": rows, **facets})
        return Resp({"total": 900, "jobPostings": [{"title": "Java Developer", "externalPath": "/job/pune/1",
                                                   "locationsText": "Pune, Gera Commerzone SEZ"}], **facets})


def test_workday_asks_for_germany_only():
    s = FacetSession({})
    jobs = ats.workday(s, "Cisco", "https://cisco.wd5.myworkdayjobs.com/Cisco_Careers")
    assert [j.title for j in jobs] == ["Python Developer", "Data Engineer", "ML Engineer"]
    assert jobs[1].location == "2 Locations (Germany)"
    assert filters.location_ok(jobs[1], CFG["filters"])


def test_real_location_from_the_detail_is_checked_again():
    f = CFG["filters"]
    j = mk("Python Developer", loc="3 Locations", desc="", full=False)

    def fetch(cands):
        for c in cands:
            c.location = "Pune · Kraków · Bucharest"           # what the job's detail page says
            c.description = "Python, FastAPI, Docker." + FILLER
    res = filters.classify([j], f, 2, fetch_descriptions=fetch)
    assert res.matched == [] and "not in Germany" in res.rejected[0][1]


def test_eightfold_ericsson():
    page1 = {"status": 200, "data": {"positions": [
        {"id": 563121777546816, "name": "Graduate AI/ML and Data Engineer (m/f/d)",
         "locations": ["Aachen,North Rhine Westphalia,Germany"], "postedTs": 1793618939,
         "positionUrl": "/careers/job/563121777546816", "workLocationOption": "onsite"},
        {"id": 563121774614565, "name": "Senior RF Developer",
         "locations": ["Munich,Bavaria,Germany", "Pune,Maharashtra,India"], "postedTs": 1790584707,
         "positionUrl": "/careers/job/563121774614565"}]}}
    s = FakeSession({"jobs.ericsson.com/api/pcsx/search": lambda params: Resp(page1 if params["start"] == 0
                                                                            else {"data": {"positions": []}})})
    jobs = ats.eightfold(s, "Ericsson", "jobs.ericsson.com")
    assert [j.title for j in jobs] == ["Graduate AI/ML and Data Engineer (m/f/d)", "Senior RF Developer"]
    g = jobs[0]
    assert g.url == "https://jobs.ericsson.com/careers/job/563121777546816"
    assert "position_id=563121777546816&domain=ericsson.com" in g.detail_url
    assert g.location == "Aachen, North Rhine Westphalia, Germany" and g.posted.year == 2026
    assert s.calls[0][1]["location"] == "Germany" and s.calls[0][1]["domain"] == "ericsson.com"
    assert filters.location_ok(jobs[1], CFG["filters"])          # Munich · Pune: has a German office


def test_excluded_companies_never_show_up():
    from jobfinder import filters
    cfg = {**CFG["filters"], "exclude_companies": ["Microsoft"]}
    mk = lambda c: Job(source="linkedin", company=c, title="Python Developer", url="https://x/" + c,
                       location="Berlin, Germany", description="Python, FastAPI, Docker." + FILLER)
    res = filters.classify([mk("Microsoft"), mk("Microsoft Deutschland GmbH"), mk("Microsoftware AG"), mk("Acme")],
                           cfg, 2)
    kept = [j.company for j in res.matched + res.low + res.unchecked]
    assert "Microsoft" not in kept and "Microsoft Deutschland GmbH" not in kept
    assert "Microsoftware AG" in kept and "Acme" in kept and res.stats["company"] == 2


def test_personio_search_json_when_xml_is_gone():
    rows = [{"id": 2878647, "name": "(Sr.) Linux / DevOps Engineer (w/m/d)", "office": "Berlin",
             "offices": ["Berlin"], "description": ""}]
    s = FakeSession({"zertificon.jobs.personio.de/xml": Resp("x", 404),
                     "zertificon.jobs.personio.com/xml": Resp("x", 404),
                     "zertificon.jobs.personio.de/search.json": Resp(rows)})
    jobs = ats.personio(s, "Zertificon", "zertificon")
    assert [(j.title, j.location, j.url) for j in jobs] == [
        ("(Sr.) Linux / DevOps Engineer (w/m/d)", "Berlin",
         "https://zertificon.jobs.personio.de/job/2878647?language=en")]


def test_personio_empty_page_is_a_failure_not_zero_jobs():
    s = FakeSession({"lector.jobs.personio.de/": Resp("<html><div id='root'></div></html>")})
    try:
        ats.personio(s, "Lector", "lector")
        assert False, "should fail"
    except ats.FeedError as e:
        assert "could not be read" in str(e) and "search.json" in str(e)
    # a page that really says there are no openings is a real 0
    s = FakeSession({"calm.jobs.personio.de/": Resp("<html>Currently no open positions</html>")})
    assert ats.personio(s, "Calm", "calm") == []
