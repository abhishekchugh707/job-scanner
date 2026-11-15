"""India tab: Indian places go to their own tab, Germany stays as it is."""
import copy
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from jobfinder import filters  # noqa: E402
from jobfinder.models import Job  # noqa: E402
from jobfinder.sources import boards  # noqa: E402
from test_jobfinder import CFG, FILLER, mk  # noqa: E402

F = copy.deepcopy(CFG["filters"])
F["india"] = True


def test_indian_places_go_to_india_and_germany_is_unchanged():
    for loc in ["Bengaluru, Karnataka, India", "Hyderabad", "Pune · Mumbai", "Gurugram, Haryana", "Remote, India"]:
        j = mk("x", loc)
        assert filters.location_ok(j, F) and j.india, loc
    for loc in ["Köln", "Berlin · Bengaluru"]:                 # an office in Germany: the German tabs
        j = mk("x", loc)
        assert filters.location_ok(j, F) and not j.india, loc
    for loc in ["London, UK", "Indianapolis, Indiana", "Singapore"]:
        assert not filters.location_ok(mk("x", loc), F), loc
    no = copy.deepcopy(F)
    no["india"] = False
    assert not filters.location_ok(mk("x", "Bengaluru, India"), no)


def test_india_jobs_need_no_german_check_but_the_same_role_rules():
    ok = mk("Python Backend Engineer", "Bengaluru, India", desc="Skills: python, fastapi, docker")   # Naukri: tags only
    no = mk("Sales Manager", "Pune, India", desc="Skills: sales")
    sen = mk("Senior Python Developer", "Mumbai, India", desc="Python " + FILLER)
    res = filters.classify([ok, no, sen], F, 2)
    assert ok in res.matched and ok.india and not any("German" in f for f in ok.flags)
    assert no not in res.matched + res.maybe + res.unchecked
    assert sen not in res.matched


def test_priority_cities():
    assert filters.india_priority(mk("x", "Hyderabad, Telangana"), F)
    assert not filters.india_priority(mk("x", "Chennai, Tamil Nadu"), F)


def test_india_board_marks_jobs_and_works_without_naukri(monkeypatch):
    import pandas as pd
    calls = []

    def fake_scrape(site_name, search_term, location, country_indeed, **kw):
        calls.append((site_name[0], location, country_indeed))
        return pd.DataFrame([{"site": site_name[0], "company": "Acme", "title": "ML Engineer", "job_url": f"https://x/{site_name[0]}/{search_term}",
                              "location": "Pune, IN", "description": None, "skills": ["python", "pytorch"], "experience_range": "2-5 Yrs"}])
    import types
    monkeypatch.setitem(sys.modules, "jobspy", types.SimpleNamespace(scrape_jobs=fake_scrape))
    monkeypatch.setattr(boards, "jobspy_has_naukri", lambda: False)
    monkeypatch.setattr(boards.time, "sleep", lambda s: None)
    cfg = {"sites": ["linkedin", "naukri", "indeed"], "keywords": ["ML Engineer"], "locations": ["India"]}
    jobs = boards.jobspy_india(None, cfg)
    assert {c[0] for c in calls} == {"linkedin", "indeed"} and all(c[2] == "India" for c in calls)   # old jobspy: no naukri
    assert all(j.country == "IN" for j in jobs)
    assert "Skills: python, pytorch" in jobs[0].description and "Years of experience: 2-5" in jobs[0].description


def test_broad_keywords_skip_linkedin(monkeypatch):
    import pandas as pd, types
    calls = []

    def fake_scrape(site_name, search_term, location, country_indeed, **kw):
        calls.append((site_name[0], search_term))
        return pd.DataFrame([])
    monkeypatch.setitem(sys.modules, "jobspy", types.SimpleNamespace(scrape_jobs=fake_scrape))
    monkeypatch.setattr(boards.time, "sleep", lambda s: None)
    boards.jobspy(None, {"sites": ["linkedin", "indeed"], "keywords": ["MLOps Engineer"],
                         "broad_keywords": ["Software Developer"]})
    assert ("linkedin", "MLOps Engineer") in calls and ("indeed", "MLOps Engineer") in calls
    assert ("indeed", "Software Developer") in calls and ("linkedin", "Software Developer") not in calls


def test_too_many_years_are_dropped_in_india():
    young = mk("Python Developer", "Pune, India", desc="Skills: python, django\nYears of experience: 2-5")
    old = mk("Python Developer II", "Pune, India", desc="Skills: python, django\nYears of experience: 8-12")
    res = filters.classify([young, old], F, 2)
    assert young in res.matched
    assert any(j is old and "at least 8 years" in r for j, r in res.rejected)


def test_keyword_yield_counts_what_only_one_search_found(tmp_path):
    from jobfinder import scan
    from jobfinder.db import JobDB
    db = JobDB(tmp_path / "jobs.db")
    a = mk("ML Engineer", "Berlin"); a.found_by = ["LinkedIn: Machine Learning Engineer"]
    b = mk("Data Engineer", "Köln"); b.found_by = ["Indeed: Data Engineer"]
    b2 = mk("Data Engineer", "Köln"); b2.source = "greenhouse"            # same job on the careers page
    cfg = {"boards": {"jobspy": {"enabled": True, "sites": ["linkedin", "indeed"], "keywords": ["Machine Learning Engineer"],
                                 "broad_keywords": ["Software Developer"]}}}
    rows = scan.keyword_yield(db, [a, b, b2], {"main": [a, b]}, cfg, "2026-11-09")
    got = {r["search"]: (r["reached"], r["unique"]) for r in rows}
    assert got["LinkedIn: Machine Learning Engineer"] == (1, 1)
    assert got["Indeed: Data Engineer"] == (1, 0)
    assert got["Indeed: Software Developer"] == (0, 0) and "LinkedIn: Software Developer" not in got


def test_dotnet_goes_low_and_java_stays_in_india():
    dn = mk("Dot Net Fullstack Developer", "Hyderabad, India", desc="Skills: .net, c#, angular, sql\nYears of experience: 2-5")
    java = mk("Java Full Stack Developer", "Bengaluru, India", desc="Skills: java, spring, react, sql\nYears of experience: 2-5")
    mixed = mk("Backend Developer", "Pune, India", desc="Skills: python, django, .net\nYears of experience: 2-5")
    res = filters.classify([dn, java, mixed], F, 2)
    assert dn in res.low and dn not in res.matched
    assert java not in res.low                           # Java you keep
    assert not any("moved to LOW" in f for f in mixed.flags)   # Python is there: not the .NET rule


def test_indeed_state_codes_count_as_priority():
    for loc in ["KA, India", "TS, India", "MH, India", "Pune Division, Maharashtra, India"]:
        assert filters.india_priority(mk("x", loc), F), loc
    assert not filters.india_priority(mk("x", "TN, India"), F)


def test_stored_india_jobs_are_re_sorted(tmp_path):
    from jobfinder import scan
    from jobfinder.db import JobDB
    db = JobDB(tmp_path / "jobs.db")
    dn = mk("Dot Net Developer", "Hyderabad, India", desc="Skills: c#, sql")
    ka = mk("Python Developer", "KA, India", desc="Skills: python")
    db.add([dn], "india", {dn.key: {"india": True, "prio": True}})
    db.add([ka], "india", {ka.key: {"india": True, "prio": False}})
    assert scan.refresh_india(db, F) == 2
    got = {r["key"]: (r["grp"], r["status"], json.loads(r["data"])["prio"]) for r in db.con.execute("SELECT * FROM jobs")}
    assert got[dn.key][:2] == ("india_low", "seen") and got[ka.key] == ("india", "new", True)


def test_fast_india_lane_uses_the_same_keywords():
    from jobfinder import scan
    b = {"jobspy_india": {"sites": ["linkedin"], "keywords": ["AI Engineer"], "broad_keywords": ["Software Engineer"],
                          "locations": ["India"], "hours_old": 24},
         "jobspy_india_fast": {"sites": ["naukri", "indeed"], "min_hours_between_runs": 1}}
    scan.india_lane(b)
    f = b["jobspy_india_fast"]
    assert f["keywords"] == ["AI Engineer"] and f["broad_keywords"] == ["Software Engineer"] and f["hours_old"] == 24
    assert f["sites"] == ["naukri", "indeed"] and b["jobspy_india"]["sites"] == ["linkedin"]
