import sys
import types
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
sys.path.insert(0, str(Path(__file__).resolve().parent))

import yaml

from jobfinder import discover
from jobfinder.models import Job
from jobfinder.sources import boards
from test_jobfinder import ISO, FakeSession, Resp

FEED = Resp({"jobs": [{"id": 1, "title": "ML Engineer", "absolute_url": "https://job-boards.greenhouse.io/acme/jobs/1",
                       "location": {"name": "Köln"}, "first_published": ISO(1), "content": "Python"}]})


def test_company_from_google_jobs_is_added_and_read_directly(tmp_path):
    (tmp_path / "companies.yaml").write_text("- {name: N26, ats: greenhouse, id: n26}\n")
    jobs = [Job(source="google", company="Acme Robotics GmbH", title="ML Engineer",
                url="https://job-boards.greenhouse.io/acme/jobs/1"),
            Job(source="indeed", company="Beta AG", title="Data Engineer", url="https://de.indeed.com/viewjob?jk=1",
                apply_url="https://beta.jobs.personio.de/job/42"),
            Job(source="linkedin", company="N26", title="Backend Engineer",            # already known
                url="https://www.linkedin.com/jobs/view/1", apply_url="https://job-boards.greenhouse.io/n26/jobs/9"),
            Job(source="greenhouse", company="Gamma", title="x",                        # not from a board
                url="https://job-boards.greenhouse.io/gamma/jobs/1")]
    s = FakeSession({"boards/acme/jobs": FEED})           # Beta's personio feed fails -> not added
    added = discover.discover(s, jobs, discover.load_companies(tmp_path), tmp_path, log=lambda m: None)
    assert [(a["name"], a["ats"], a["id"]) for a in added] == [("Acme Robotics GmbH", "greenhouse", "acme")]
    names = [c["name"] for c in discover.load_companies(tmp_path)]
    assert names == ["N26", "Acme Robotics GmbH"]
    # next scan: nothing is tested twice
    s2 = FakeSession({})
    assert discover.discover(s2, jobs, discover.load_companies(tmp_path), tmp_path, log=lambda m: None) == []
    assert s2.calls == []
    assert yaml.safe_load((tmp_path / "companies_auto.yaml").read_text())[0]["found_via"].startswith("google")


def test_jobspy_reads_google_with_its_own_search_term(monkeypatch):
    import pandas as pd
    calls = []

    def scrape_jobs(**kw):
        calls.append(kw)
        site = kw["site_name"][0]
        return pd.DataFrame([{"site": site, "company": "Acme", "title": "MLOps Engineer", "job_url": f"https://{site}/1",
                              "location": "Köln, Germany", "date_posted": "2026-10-20", "description": "Python",
                              "is_remote": False, "job_url_direct": "https://acme.jobs.personio.de/job/1"}])
    monkeypatch.setitem(sys.modules, "jobspy", types.SimpleNamespace(scrape_jobs=scrape_jobs))
    monkeypatch.setattr(boards.time, "sleep", lambda s: None)
    jobs = boards.jobspy(None, {"sites": ["indeed", "google"], "keywords": ["MLOps Engineer"],
                                "results": {"indeed": 100, "google": 50}, "hours_old": 48})
    assert [c["results_wanted"] for c in calls] == [100, 50]
    assert calls[1]["google_search_term"] == "MLOps Engineer jobs in Germany since yesterday"
    g = [j for j in jobs if j.source == "google"][0]
    assert g.posted is None                      # jobspy's wrong "days" for Google is dropped
    assert jobs[0].apply_url == "https://acme.jobs.personio.de/job/1"


def test_company_seen_on_linkedin_is_looked_up_by_name(tmp_path, monkeypatch):
    from jobfinder import network
    looked = []

    def fake_find(session, name):
        looked.append(name)
        return {"ats": "eightfold", "id": "jobs.ericsson.com"} if name == "Ericsson" else None
    monkeypatch.setattr(network, "find_careers_page", fake_find)
    (tmp_path / "companies.yaml").write_text("- {name: N26, ats: greenhouse, id: n26}\n")
    jobs = [Job(source="linkedin", company="Ericsson", title="Graduate AI/ML and Data Engineer",
                url="https://www.linkedin.com/jobs/view/4485472778"),
            Job(source="linkedin", company="N26", title="Backend Engineer", url="https://www.linkedin.com/jobs/view/1"),
            Job(source="arbeitsagentur", company="Kleine GmbH", title="Python Entwickler", url="https://ba/1")]
    assert discover.discover(FakeSession({}), jobs, discover.load_companies(tmp_path), tmp_path,
                             log=lambda m: None) == []                # the scan only queues them ...
    added = discover.lookup_queued(FakeSession({}), tmp_path, log=lambda m: None)   # ... looked up in the background
    assert [(a["name"], a["ats"], a["id"]) for a in added] == [("Ericsson", "eightfold", "jobs.ericsson.com")]
    assert looked == ["Ericsson", "Kleine GmbH"]                      # N26 is known already
    assert [c["name"] for c in discover.load_companies(tmp_path)] == ["N26", "Ericsson"]
    discover.discover(FakeSession({}), jobs, discover.load_companies(tmp_path), tmp_path, log=lambda m: None)
    discover.lookup_queued(FakeSession({}), tmp_path, log=lambda m: None)
    assert looked == ["Ericsson", "Kleine GmbH"]                      # each company looked up once
