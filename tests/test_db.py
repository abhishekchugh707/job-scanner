import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from jobfinder.db import JobDB, fingerprint
from jobfinder.models import Job


def J(company, title, url, source="greenhouse"):
    return Job(source=source, company=company, title=title, url=url, location="Köln")


def test_applied_job_from_careers_page_stays_hidden_on_linkedin(tmp_path):
    db = JobDB(tmp_path / "jobs.db")
    a = J("Jupus", "Full Stack Developer (Angular / Python)", "https://jobs.ashbyhq.com/jupus/2")
    assert db.add([a], "main") == 1
    db.set_status(a.key, "applied")
    li = J("JUPUS GmbH", "Full Stack Developer (Angular/Python) (m/w/d)",
           "https://www.linkedin.com/jobs/view/4012345678", "linkedin")
    assert not db.is_new(li)                      # the scanner won't add it again
    assert db.add([li], "main") == 0
    shown = db.jobs()
    assert len(shown) == 1 and shown[0]["status"] == "applied"


def test_different_roles_at_same_company_stay_separate(tmp_path):
    db = JobDB(tmp_path / "jobs.db")
    py = J("N26", "Software Engineer (Python)", "https://x/1")
    go = J("N26", "Software Engineer (Go)", "https://x/2")
    assert fingerprint(py.company, py.title) != fingerprint(go.company, go.title)
    db.add([py], "main")
    db.set_status(py.key, "skipped")
    assert db.is_new(go)
    assert db.add([go], "main") == 1
    st = {d["title"]: d["status"] for d in db.jobs()}
    assert st == {"Software Engineer (Python)": "skipped", "Software Engineer (Go)": "new"}


def test_job_logged_by_hand_is_never_shown_as_new(tmp_path):
    db = JobDB(tmp_path / "jobs.db")
    db.add_manual("Vattenfall", "MLOps Engineer", "https://www.linkedin.com/jobs/view/1")
    later = J("Vattenfall GmbH", "MLOps Engineer (f/m/d)", "https://job-boards.greenhouse.io/vattenfall/jobs/4")
    assert not db.is_new(later)


def test_status_and_new_counts(tmp_path):
    db = JobDB(tmp_path / "jobs.db")
    jobs = [J("A", "Python Developer", "https://a/1"), J("B", "Data Engineer", "https://b/1")]
    db.add(jobs, "main")
    assert db.counts() == {"new": 2}
    db.add(jobs, "main")                           # seen again in the next scan: no duplicate
    assert db.counts() == {"new": 2}
    db.mark_new_as_seen()
    assert db.counts() == {"seen": 2}


def test_old_seen_file_is_imported_once(tmp_path):
    old = tmp_path / "seen_jobs.json"
    old.write_text(json.dumps({"abc": {"company": "N26", "title": "Backend Engineer (Python)",
                                       "first_seen": "2026-10-28T10:00:00+00:00"}}))
    db = JobDB(tmp_path / "jobs.db")
    assert db.import_seen_file(old) == 1
    assert db.import_seen_file(old) == 0
    again = J("N26", "Backend Engineer (Python) (m/w/d)", "https://other-link")
    assert not db.is_new(again)                    # you saw it in an earlier run
    assert db.jobs() == []                         # imported rows have no details to show


def test_jobs_seen_in_old_runs_come_back_as_seen_with_details(tmp_path):
    old = tmp_path / "seen_jobs.json"
    old.write_text(json.dumps({"abc": {"company": "N26", "title": "Backend Engineer (Python)"}}))
    db = JobDB(tmp_path / "jobs.db")
    db.import_seen_file(old)
    j = J("N26", "Backend Engineer (Python)", "https://job-boards.greenhouse.io/n26/jobs/1")
    assert db.add([j], "main", {j.key: {"km": 478}}) == 0          # not new
    shown = db.jobs()
    assert [(d["title"], d["status"], d["km"]) for d in shown] == [("Backend Engineer (Python)", "seen", 478)]


def test_unchecked_job_moves_to_inbox_once_its_description_is_read(tmp_path):
    db = JobDB(tmp_path / "jobs.db")
    j = J("Acme", "Data Engineer", "https://www.linkedin.com/jobs/view/5")
    db.add([j], "unchecked")
    assert db.needs_text(j)
    db.add([j], "unchecked")
    db.add([j], "unchecked")                     # third failed try: stop asking
    assert not db.needs_text(j)
    db.add([j], "main")                          # read after all (e.g. via another scan)
    assert [d["group"] for d in db.jobs()] == ["main"]
    other = J("Beta", "ML Engineer", "https://x/1")
    db.add([other], "main")
    assert not db.needs_text(other)


def test_unchecked_job_gets_its_text_when_found_under_a_new_link(tmp_path):
    db = JobDB(tmp_path / "jobs.db")
    dead = J("d.velop", "Software Development Engineer (w/m/d)", "https://d-velop.jobs.personio.de/job/1", "personio")
    db.add([dead], "unchecked")
    db.set_status(dead.key, "saved")
    fixed = J("d.velop", "Software Development Engineer (w/m/d)", "https://www.google.com/search?q=d.velop", "personio")
    fixed.description = "Backend mit Python"
    assert db.add([fixed], "main") == 0                # not new: no second ping
    shown = db.jobs()
    assert len(shown) == 1 and shown[0]["group"] == "main" and shown[0]["status"] == "saved"
    assert shown[0]["url"].startswith("https://www.google.com") and shown[0]["description"] == "Backend mit Python"


def test_job_missing_from_its_feed_twice_is_marked_closed(tmp_path):
    db = JobDB(tmp_path / "jobs.db")
    a = J("Acme", "Python Developer", "https://x/1")
    b = J("Acme", "Data Engineer", "https://x/2")
    for j in (a, b):
        j.origin = "Acme (greenhouse)"
    db.add([a, b], "main")
    fa = fingerprint(a.company, a.title)
    listed = {"Acme (greenhouse)": {fa}}                  # b is gone from the feed
    assert db.update_listed(listed) == 0                  # one miss: could be a hiccup
    assert db.update_listed(listed) == 1                  # second miss: closed
    rows = {d["title"]: d for d in db.jobs()}
    assert rows["Data Engineer"]["closed"] and not rows["Python Developer"].get("closed")
    db.update_listed({"Other (lever)": set()})            # other feeds don't touch it
    db.update_listed({"Acme (greenhouse)": {fa, fingerprint(b.company, b.title)}})   # back again
    assert not {d["title"]: d for d in db.jobs()}["Data Engineer"]["closed"]
    db.set_status(b.key, "applied")                       # applied jobs are left alone
    db.update_listed(listed); db.update_listed(listed)
    assert not {d["title"]: d for d in db.jobs()}["Data Engineer"]["closed"]
