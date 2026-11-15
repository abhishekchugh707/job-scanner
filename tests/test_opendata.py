import gzip
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
sys.path.insert(0, str(Path(__file__).resolve().parent))

import yaml

from jobfinder import opendata
from test_jobfinder import CFG, FakeSession, Resp


class Bin:
    def __init__(self, data):
        self.content, self.status_code = data, 200


def test_finds_companies_hiring_your_roles_in_germany(tmp_path):
    recs = [
        {"company": "acme-ai", "title": "Machine Learning Engineer", "location": "Berlin, Germany", "ats": "Greenhouse",
         "url": "https://job-boards.greenhouse.io/acme-ai/jobs/1"},
        {"company": "acme-ai", "title": "Python Developer", "location": "Köln", "ats": "Greenhouse", "url": "x"},
        {"company": "usco", "title": "Backend Engineer", "location": "Austin, TX", "ats": "Lever", "url": "y"},
        {"company": "salesy", "title": "Sales Manager", "location": "Munich, Germany", "ats": "Ashby", "url": "z"},
        {"company": "n26", "title": "Backend Engineer", "location": "Berlin", "ats": "Greenhouse", "url": "w"},
        {"company": "caci", "title": "Software Engineer", "location": "Berlin, CT, United States", "ats": "Lever",
         "url": "v"},
        {"company": "Big Corp", "title": "Data Engineer", "location": "Hamburg, Germany", "ats": "Workday",
         "url": "https://bigcorp.wd3.myworkdayjobs.com/Careers/job/Hamburg/Data-Engineer_R1"},
    ]
    s = FakeSession({"jobs_manifest.json": Resp({"chunks": ["c0.json.gz"], "totalJobs": 6, "last_updated": "x"}),
                     "c0.json.gz": Bin(gzip.compress(json.dumps(recs).encode()))})
    od = opendata.OpenData(tmp_path, lambda: s, log=lambda m: None)
    added = od.run(CFG["filters"], [{"name": "N26", "ats": "greenhouse", "id": "n26"}])
    assert [(a["name"], a["ats"], a["id"]) for a in added] == [
        ("Acme Ai", "greenhouse", "acme-ai"),
        ("Big Corp", "workday", "https://bigcorp.wd3.myworkdayjobs.com/Careers")]
    assert "2 matching jobs" in added[0]["why"]
    assert len(yaml.safe_load((tmp_path / opendata.OUT_FILE).read_text())) == 2
    assert not od.due()                                  # weekly
    again = od._run(CFG["filters"], [{"name": "N26", "ats": "greenhouse", "id": "n26"}] + added)
    assert len(again) == 2                               # rebuilt, not doubled


def test_moved_boards_are_not_added_again(tmp_path):
    from jobfinder.db import JobDB
    recs = [{"company": "doctolib", "title": "Backend Engineer", "location": "Berlin, Germany", "ats": "Greenhouse",
             "url": "https://job-boards.greenhouse.io/doctolib/jobs/1"},
            {"company": "acme-ai", "title": "Backend Engineer", "location": "Berlin, Germany", "ats": "Greenhouse",
             "url": "https://job-boards.greenhouse.io/acme-ai/jobs/2"}]
    db = JobDB(tmp_path / "jobs.db")
    db.put("dead_boards", {"greenhouse:doctolib": {"n": 4, "last": "2026-11-06T10:00:00+00:00"},
                           "greenhouse:acme-ai": {"n": 1}})        # only one miss: still added
    db.con.close()
    s = FakeSession({"jobs_manifest.json": Resp({"chunks": ["c0.json.gz"], "totalJobs": 2}),
                     "c0.json.gz": Bin(gzip.compress(json.dumps(recs).encode()))})
    added = opendata.OpenData(tmp_path, lambda: s, log=lambda m: None).run(CFG["filters"], [])
    assert [a["id"] for a in added] == ["acme-ai"]
