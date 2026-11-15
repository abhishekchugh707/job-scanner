"""Titles without your role words: judged on skills (+ the model's second opinion); careers-page
lookup order; rejected jobs kept; 'why didn't I see this?'."""
import json
import sys
from datetime import date, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from jobfinder import discover, filters, opendata, semantic, why
from jobfinder.db import JobDB
from test_jobfinder import CFG, FILLER, FakeSession, Resp, mk

F = CFG["filters"]
ARX = ("Several years of experience in MLOps, DevOps, platform engineering. Solid Python skills and experience "
       "with containerization (Docker) and orchestration (Kubernetes). Hands-on experience with CI/CD pipelines. "
       "Understanding of data pipelines and APIs. Fluency in English; German is a plus.")


def test_decide():
    assert semantic.decide(14, None, None, 6) == "inbox"        # clearly your skills
    assert semantic.decide(4, None, None, 2) == "low"            # some of your skills: see it, no ping
    assert semantic.decide(3, None, None, 1) == "low"            # one technology: still shown
    assert semantic.decide(3, None, None, 0) is None             # a domain word alone ("Energie")
    assert semantic.decide(1, 0.86, 0.85, 1) == "inbox"          # the model lifts a hidden match
    assert semantic.decide(0, 0.90, 0.85, 0) is None             # ...but not without any of your tech
    assert semantic.decide(-4, 0.90, 0.85, 2) is None            # never against "not your stack"
    assert semantic.decide(1, 0.84, 0.85, 1) == "low"            # under the cut-off: keywords decide


def test_title_without_role_word_is_judged_on_skills():
    jobs = [mk("AI Operations Engineer (m/f/d)", desc=ARX),
            mk("IT Operations Engineer", desc="Docker, Kubernetes and Linux servers. "),
            mk("Steuerberater (m/w/d)", desc="Jahresabschlüsse und Steuererklärungen. "),
            mk("Senior Platform Engineer", desc=ARX)]                              # excluded word: as before
    res = filters.classify(jobs, F, 2)
    assert [j.title for j in res.matched] == ["AI Operations Engineer (m/f/d)"]
    assert "found by skills" in res.matched[0].flags[0]
    assert [j.title for j in res.low] == ["IT Operations Engineer"]
    reasons = {j.title: r for j, r in res.rejected}
    assert reasons["Steuerberater (m/w/d)"].startswith("title has none of your role words (skill points 0")
    assert reasons["Senior Platform Engineer"] == "title contains 'senior'"


def test_model_second_opinion_lifts_a_job_worded_differently():
    j = mk("Security Engineer", desc="Harden our model serving stack and the CI/CD pipelines that ship it. ")
    judge = lambda pts, title, desc, n: (semantic.decide(pts, 0.88, 0.85, n), 0.88)
    res = filters.classify([j], dict(F, skills_inbox_min_points=0), 2, judge=judge)     # the model's part alone
    assert res.matched and "model similarity 0.88" in res.matched[0].flags[0]
    assert not filters.classify([mk("Security Engineer", desc="Firewalls and CI/CD. ")], F, 2).matched


def test_role_word_jobs_unchanged_and_broken_model_is_harmless():
    def boom(*a):
        raise RuntimeError("model crashed")
    jobs = [mk("Python Developer"), mk("AI Operations Engineer", desc=ARX)]
    res = filters.classify(jobs, F, 2, judge=boom)
    assert {j.title for j in res.matched} == {"Python Developer", "AI Operations Engineer"}


def test_descriptions_fetched_for_skill_candidates_tech_titles_first():
    fetched = []

    def fetch(jobs):
        fetched.extend(j.title for j in jobs)
    jobs = [mk("Kaufmann (m/w/d)", full=False), mk("Cloud Operations Specialist", full=False)]
    filters.classify(jobs, {**F, "skills_fetch_limit": 1}, 2, fetch_descriptions=fetch)
    assert fetched == ["Cloud Operations Specialist"]


def test_calibration_picks_cutoff_on_your_answers(tmp_path, monkeypatch):
    data = {"hard": [{"n": 1, "title": "AI Ops", "description": "a", "yes": True},
                     {"n": 2, "title": "Security", "description": "b", "yes": True},
                     {"n": 3, "title": "IT Support", "description": "c", "yes": False},
                     {"n": 4, "title": "Field Engineer", "description": "d", "yes": False}],
            "unrelated": [{"title": f"Sales {i}", "description": "x"} for i in range(40)]}
    p = tmp_path / "cal.json"
    p.write_text(json.dumps(data))
    monkeypatch.setattr(semantic, "DATA", p)
    sims = {"AI Ops": 0.88, "Security": 0.86, "IT Support": 0.82, "Field Engineer": 0.79}
    m = semantic.Semantic(tmp_path, log=lambda s: None)
    m.similarity = lambda title, desc: sims.get(title, 0.75)
    for h in data["hard"]:
        h["description"] = "Python and Docker. " if h["yes"] else "Python. "
    p.write_text(json.dumps(data))
    cal = m.calibrate(CFG, None)
    assert cal["agree"] == 4 and 0.82 < cal["t_inbox"] <= 0.86 and cal["unrelated_over"] == 0
    assert m.calibrate(CFG, None)["sig"] == cal["sig"]                     # cached


def test_rejected_jobs_kept_and_text_reused(tmp_path):
    db = JobDB(tmp_path / "jobs.db")
    j = mk("Steuerberater (m/w/d)", desc="Steuern. ")
    db.add_rejected([(j, "title has none of your role words (skill points 0)")])
    hit = db.find(url=j.url)
    assert hit["rejected"]["reason"].startswith("title has none")
    assert db.known_text(mk("Steuerberater (m/w/d)", full=False)).startswith("Steuern")


def test_retry_failed_lookups_after_30_days():
    old = (date.today() - timedelta(days=31)).isoformat()
    assert discover._due({"name:arx": old}, "name:arx")
    assert not discover._due({"name:arx": date.today().isoformat()}, "name:arx")
    assert discover._due({}, "name:arx")


def test_careers_page_found_via_website_then_index(tmp_path):
    from jobfinder import network
    home = '<a href="/karriere">Karriere</a>'
    careers = '<iframe src="https://job-boards.eu.greenhouse.io/embed/job_board?for=arxroboticsgmbh"></iframe>'
    s = FakeSession({"arx-robotics.com/karriere": Resp(careers), "arx-robotics.com": Resp(home),
                     "boards/arxroboticsgmbh/jobs": Resp({"jobs": []})})
    res = discover._find(s, tmp_path, {"name": "ARX Robotics", "site": "arx-robotics.com"}, print)
    assert (res["ats"], res["id"], res["via"]) == ("greenhouse", "arxroboticsgmbh", "website")
    # no website: the open-data name index
    (tmp_path / opendata.INDEX_FILE).write_text(json.dumps({"arx robotics": ["greenhouse", "arxroboticsgmbh"]}))
    res = discover._find(s, tmp_path, {"name": "ARX Robotics GmbH"}, print)
    assert res["via"] == "open-data index" and res["id"] == "arxroboticsgmbh"


def test_why_explains_shown_filtered_and_unknown(tmp_path):
    db = JobDB(tmp_path / "jobs.db")
    shown = mk("Python Developer")
    db.add([shown], "main")
    db.set_status(shown.key, "skipped")
    out = why.explain(tmp_path, db, FakeSession({}), shown.url, CFG)
    assert out["verdict"] == "shown" and "Back to inbox" in out["text"]
    gone = mk("Kaufmann (m/w/d)")
    db.add_rejected([(gone, "title has none of your role words (skill points 0)")])
    assert why.explain(tmp_path, db, FakeSession({}), gone.url, CFG)["verdict"] == "filtered"
    out = why.explain(tmp_path, db, FakeSession({}), "https://www.linkedin.com/jobs/view/4486260436", CFG)
    assert out["verdict"] == "never seen" and "LinkedIn" in out["text"]
    assert len(why.cases(tmp_path)) == 3


def test_why_adds_a_missing_careers_page_and_checks_the_job(tmp_path):
    db = JobDB(tmp_path / "jobs.db")
    (tmp_path / "companies.yaml").write_text("- {name: Acme, ats: lever, id: acme}\n")
    feed = {"jobs": [{"id": 5029805101, "title": "AI Operations Engineer (m/f/d)",
                      "absolute_url": "https://job-boards.eu.greenhouse.io/arxroboticsgmbh/jobs/5029805101",
                      "location": {"name": "Munich"}, "first_published": "2026-11-06T08:28:43-05:00",
                      "content": "<p>" + ARX + FILLER + "</p>"}]}
    s = FakeSession({"boards/arxroboticsgmbh/jobs": Resp(feed)})
    out = why.explain(tmp_path, db, s, "https://job-boards.eu.greenhouse.io/arxroboticsgmbh/jobs/5029805101?gh_src=x", CFG)
    assert "is now" in out["steps"][0]
    assert "would reach your Inbox" in out["steps"][1]
    assert "arxroboticsgmbh" in (tmp_path / discover.AUTO_FILE).read_text()


def test_domain_word_alone_doesnt_count():
    cnc = mk("CNC-Zerspanungsmechaniker (m/w/d)", desc="Wir sind ein Unternehmen der Energie- und Erneuerbare-Energien-Branche. ")
    ops = mk("IT Operations Engineer", desc="Docker, Kubernetes and Linux servers. ")
    one = mk("Konstruktionsingenieur (m/w/d)", desc="Auswertungen mit Python. ")
    res = filters.classify([cnc, ops, one], F, 2)
    assert {j.title for j in res.low} == {"IT Operations Engineer", "Konstruktionsingenieur (m/w/d)"}
    assert res.rejected[0][1].startswith("title has none of your role words (skill points")
    # the model can't lift an ad that names none of your technologies
    res = filters.classify([mk("CNC", desc="Energie. ")], F, 2,
                           judge=lambda p, t, d, n: (semantic.decide(p, 0.9, 0.85, n), 0.9))
    assert not res.matched and not res.low


def test_unreadable_description_goes_to_low_match():
    res = filters.classify([mk("Cloud Operations Specialist", full=False)], F, 2)
    assert res.low and "couldn't be read" in res.low[0].flags[-1]


def test_workday_board_without_germany_filter_needs_a_known_place():
    from jobfinder.models import Job
    sg = Job(source="workday", company="sggovterp", title="AI Engineer", url="u", location="EMA HQ", strict_location=True)
    de = Job(source="workday", company="X", title="AI Engineer", url="u", location="Berlin", strict_location=True)
    loose = Job(source="lever", company="X", title="AI Engineer", url="u", location="EMA HQ")
    assert not filters.location_ok(sg, F) and filters.location_ok(de, F) and filters.location_ok(loose, F)


def test_write_atomic_parallel_writers(tmp_path):
    import threading
    from jobfinder.util import write_atomic
    p = tmp_path / "q.json"
    errors = []

    def w(i):
        try:
            for _ in range(30):
                write_atomic(p, str(i))
        except Exception as e:
            errors.append(e)
    ts = [threading.Thread(target=w, args=(i,)) for i in range(4)]
    [t.start() for t in ts]
    [t.join() for t in ts]
    assert not errors and p.read_text() in {"0", "1", "2", "3"}


def test_name_index_matches_board_ids():
    assert opendata.squash("ARX Robotics GmbH") == opendata.squash("arxroboticsgmbh") == "arxrobotics"
    assert opendata.squash("arx-robotics") == "arxrobotics"


def test_lookup_retries_every_earlier_miss(tmp_path):
    (tmp_path / discover.TRIED_FILE).write_text(json.dumps({"name:arx robotics": date.today().isoformat(), "_v": 2}))
    discover.lookup_queued(FakeSession({}), tmp_path, log=lambda m: None)
    tried = json.loads((tmp_path / discover.TRIED_FILE).read_text())
    assert tried["_v"] == discover.LOOKUP_VERSION


def test_silent_failures_become_warnings(tmp_path):
    from jobfinder import scan
    m = semantic.Semantic(tmp_path, log=lambda s: None)
    m.status, m.attempts = "off: download timed out (keywords decide alone)", 1
    scan.record_background_error(tmp_path, "company careers-page lookup", FileNotFoundError("queue.json.tmp"))
    w = scan.health_warnings(tmp_path, CFG, m, {"errors": 2, "first_error": "RuntimeError: boom"},
                             {"failed": 5}, False)
    text = " | ".join(w)
    assert "Skills model is off: download timed out" in text and "retrying automatically" in text
    assert "failed on 2 jobs" in text and "lookup crashed" in text and "description of 5 jobs" in text
    assert not (tmp_path / scan.BG_ERRORS).exists()                # shown once, then cleared
    m.ready = True
    assert scan.health_warnings(tmp_path, CFG, m, {}, {"failed": 0}, False) == []   # healthy: silent


def test_model_retries_with_growing_gaps(tmp_path):
    import time
    m = semantic.Semantic(tmp_path, log=lambda s: None)
    assert not m.retry_due()                                        # never failed
    m.attempts, m.failed_at = 1, time.time() - 21 * 60
    assert m.retry_due()
    m.attempts, m.failed_at = 2, time.time() - 21 * 60
    assert not m.retry_due()                                        # second failure: wait an hour


def test_model_counts_what_it_did(tmp_path):
    m = semantic.Semantic(tmp_path, log=lambda s: None)
    m.ready, m.cal = True, {"t_inbox": 0.85}
    m.similarity = lambda t, d: 0.9
    assert m.judge(1, "Security Engineer", "x" * 300, 1)[0] == "inbox"
    def boom(t, d):
        raise RuntimeError("onnx")
    m.similarity = boom
    assert m.judge(4, "Ops", "x" * 300, 2)[0] == "low"              # falls back to keywords
    st = m.take_stats()
    assert (st["judged"], st["lifted"], st["errors"]) == (1, 1, 1) and "onnx" in st["first_error"]
    assert m.take_stats()["judged"] == 0


def test_why_follows_a_company_careers_page_to_a_hidden_ashby_board(tmp_path):
    db = JobDB(tmp_path / "jobs.db")
    jid = "d0a2e3ab-ccf5-490e-a048-fd348206e4ca"
    page = f'<a href="https://blp.digital/company/careers?ashby_jid={jid}">Software Engineer</a>'
    board = {"jobs": [{"id": jid, "title": "Software Engineer", "location": "Cologne", "jobUrl": "https://jobs.ashbyhq.com/blp-digital/" + jid,
                       "address": {"postalAddress": {"addressCountry": "Germany"}}, "descriptionPlain": "Python"}]}
    s = FakeSession({"blp.digital/company/careers": Resp(page),
                     "posting-api/job-board/blp-digital": Resp(board),
                     "jobs.ashbyhq.com/blp-digital": Resp("<title>BLP Jobs</title>")})
    out = why.explain(tmp_path, db, s, "https://blp.digital/company/careers", CFG)
    assert out["verdict"] == "careers page", out
    assert "BLP's jobs are on Ashby" in out["text"] and "1 open, 1 in Germany" in out["text"] and "Added" in out["text"]
    assert "blp-digital" in (tmp_path / discover.AUTO_FILE).read_text()
    # a site with no platform behind it says so plainly
    out = why.explain(tmp_path, db, FakeSession({"example.org": Resp("<html>mail us</html>")}), "https://example.org/jobs", CFG)
    assert "No hiring platform" in out["text"]


def test_why_finds_the_job_you_got_from_a_board(tmp_path):
    db = JobDB(tmp_path / "jobs.db")
    from jobfinder.models import Job
    seen = Job(source="arbeitnow", company="eleQtron GmbH", title="QA Engineer Quantum Computing (m/w/d)",
               url="https://www.arbeitnow.com/jobs/companies/eleqtron/qa-engineer-123", location="Siegen")
    db.add([seen], "main")
    (tmp_path / "companies_auto.yaml").write_text("- {name: eleQtron GmbH, ats: recruitee, id: eleqtron}\n")
    feed = {"offers": [{"id": 1, "title": "QA Engineer Quantum Computing (m/w/d)", "slug": "qa-engineer-quantum-computing-mwd",
                        "careers_url": "https://eleqtron.recruitee.com/o/qa-engineer-quantum-computing-mwd",
                        "location": "Siegen", "country_code": "DE", "description": "<p>Python</p>", "published_at": "2026-11-06 10:00:00 UTC"}]}
    s = FakeSession({"eleqtron.recruitee.com/api/offers": Resp(feed)})
    out = why.explain(tmp_path, db, s, "https://eleqtron.recruitee.com/o/qa-engineer-quantum-computing-mwd?utm_source=arbeitnow.com", CFG)
    assert out["verdict"] == "shown" and "via arbeitnow" in out["text"], out


def test_why_finds_a_board_behind_a_javascript_careers_page(tmp_path):
    # no platform link in the page at all - only the company's web address and LinkedIn page
    page = '<title>Careers at BLP</title><a href="https://www.linkedin.com/company/blp-digital/">LinkedIn</a>'
    board = {"jobs": [{"id": "x1", "title": "Software Engineer", "location": "Cologne", "jobUrl": "https://jobs.ashbyhq.com/blp-digital/x1",
                       "address": {"postalAddress": {"addressCountry": "Germany"}}}]}
    s = FakeSession({"blp.digital": Resp(page), "posting-api/job-board/blp-digital": Resp(board),
                     "jobs.ashbyhq.com/blp-digital": Resp("<title>BLP Jobs</title>"),
                     "boards-api.greenhouse.io/v1/boards/blp": Resp({"jobs": [{"id": 1}]}),
                     "boards-api.greenhouse.io/v1/boards/blp": Resp({"name": "Bilfinger"})})
    out = why.explain(tmp_path, JobDB(tmp_path / "jobs.db"), s, "https://blp.digital/company/careers", CFG)
    assert "on Ashby" in out["text"] and "blp-digital" in out["text"], out
