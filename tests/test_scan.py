"""A full scan against fake feeds: nothing may fall through the gaps."""
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
sys.path.insert(0, str(Path(__file__).resolve().parent))

import yaml

from jobfinder import scan
from jobfinder.db import JobDB
from test_jobfinder import FILLER, ISO, FakeSession, Resp

ROOT = Path(__file__).resolve().parent.parent


def setup(tmp_path, days_old):
    cfg = yaml.safe_load((ROOT / "config.yaml").read_text(encoding="utf-8"))
    for b in cfg["boards"].values():
        b["enabled"] = False
    (tmp_path / "config.yaml").write_text(yaml.safe_dump(cfg, allow_unicode=True), encoding="utf-8")
    (tmp_path / "companies.yaml").write_text("- {name: Acme, ats: greenhouse, id: acme}\n")
    feed = Resp({"jobs": [{"id": 1, "title": "Python Backend Engineer", "absolute_url": "https://x/acme/1",
                           "location": {"name": "Berlin, Germany"}, "first_published": ISO(days_old),
                           "content": "Python, FastAPI, PostgreSQL, Docker, MLflow." + FILLER}]})
    return FakeSession({"boards/acme/jobs": feed})


def run(tmp_path, session, monkeypatch):
    monkeypatch.setattr(scan, "make_session", lambda: session)
    summary, res = scan.run_scan(tmp_path, log=lambda m: None)
    return summary, res


def test_pc_off_for_days_loses_nothing(tmp_path, monkeypatch):
    s = setup(tmp_path, days_old=5)                      # posted 5 days ago
    db = JobDB(tmp_path / "jobs.db")
    db.put("backfilled", ["greenhouse:acme"])            # known company ...
    six_days = (datetime.now(timezone.utc) - timedelta(days=6)).isoformat(timespec="seconds")
    db.put("last_ok", {"greenhouse:acme": six_days})     # ... last read 6 days ago (PC was off)
    db.con.close()
    summary, res = run(tmp_path, s, monkeypatch)
    assert [j.title for j in res.matched] == ["Python Backend Engineer"]
    assert any("caught up" in f for f in res.matched[0].flags)
    # next hour: read normally again, the 5-day-old job is no longer "new" (already stored)
    summary2, res2 = run(tmp_path, s, monkeypatch)
    assert summary2["added"]["main"] == 0


def test_normal_hourly_scan_ignores_old_jobs(tmp_path, monkeypatch):
    s = setup(tmp_path, days_old=5)
    db = JobDB(tmp_path / "jobs.db")
    db.put("backfilled", ["greenhouse:acme"])
    db.put("last_ok", {"greenhouse:acme": datetime.now(timezone.utc).isoformat(timespec="seconds")})
    db.con.close()
    summary, res = run(tmp_path, s, monkeypatch)
    assert res.matched == [] and summary["stats"]["too_old"] == 1


def test_crash_before_storing_keeps_the_catch_up_window(tmp_path, monkeypatch):
    s = setup(tmp_path, days_old=5)
    db = JobDB(tmp_path / "jobs.db")
    db.put("backfilled", ["greenhouse:acme"])
    old = (datetime.now(timezone.utc) - timedelta(days=6)).isoformat(timespec="seconds")
    db.put("last_ok", {"greenhouse:acme": old})
    db.con.close()
    monkeypatch.setattr(scan.places, "distance_km", lambda *a: 1 / 0)    # crash while storing
    try:
        run(tmp_path, s, monkeypatch)
    except ZeroDivisionError:
        pass
    assert JobDB(tmp_path / "jobs.db").get("last_ok")["greenhouse:acme"] == old   # not marked as read


def test_daily_lists_are_read_once_a_day(tmp_path):
    from zoneinfo import ZoneInfo
    tz = ZoneInfo("Europe/Berlin")
    db = JobDB(tmp_path / "jobs.db")
    cfg = {"daily_scan_hour": 7}
    assert scan.daily_due(db, cfg)                                         # never read: now
    db.put("daily_last_ok", datetime(2026, 11, 1, 9, 0, tzinfo=tz).isoformat())
    assert not scan.daily_due(db, cfg, datetime(2026, 11, 1, 18, 0, tzinfo=tz))   # same day
    assert not scan.daily_due(db, cfg, datetime(2026, 11, 2, 6, 30, tzinfo=tz))   # before 7:00
    assert scan.daily_due(db, cfg, datetime(2026, 11, 2, 7, 5, tzinfo=tz))        # first scan after 7
    assert scan.daily_due(db, cfg, datetime(2026, 11, 3, 2, 0, tzinfo=tz))        # >24 h: PC was off


def test_hourly_scan_skips_the_daily_lists(tmp_path, monkeypatch):
    s = setup(tmp_path, days_old=0.2)
    s.routes["boards/beta/jobs"] = Resp({"jobs": []})
    (tmp_path / "companies_network.yaml").write_text("- {name: Beta, ats: greenhouse, id: beta}\n")
    db = JobDB(tmp_path / "jobs.db")
    db.put("daily_last_ok", datetime.now(timezone.utc).isoformat(timespec="seconds"))
    db.con.close()
    summary, _ = run(tmp_path, s, monkeypatch)                # Beta is new: read at once
    assert not summary["daily"] and len(summary["sources"]) == 2
    summary, _ = run(tmp_path, s, monkeypatch)                # then only in the daily scan
    assert not summary["daily"] and [x["source"] for x in summary["sources"]] == ["Acme (greenhouse)"]
    monkeypatch.setattr(scan, "make_session", lambda: s)
    summary, _ = scan.run_scan(tmp_path, log=lambda m: None, full=True)
    assert summary["daily"] and len(summary["sources"]) == 2


def test_new_company_whose_feed_fails_is_retried_every_hour(tmp_path, monkeypatch):
    s = setup(tmp_path, days_old=0.2)
    (tmp_path / "companies_network.yaml").write_text("- {name: Gamma, ats: greenhouse, id: gamma}\n")
    db = JobDB(tmp_path / "jobs.db")
    db.put("daily_last_ok", datetime.now(timezone.utc).isoformat(timespec="seconds"))
    db.con.close()
    for _ in range(2):                                    # gamma's feed is down: tried again next hour
        summary, _ = run(tmp_path, s, monkeypatch)
        assert "Gamma (greenhouse)" in [x["source"] for x in summary["sources"]]


def test_board_that_is_gone_is_only_checked_weekly(tmp_path, monkeypatch):
    s = setup(tmp_path, days_old=0.2)
    s.routes["boards/gone/jobs"] = Resp("not found", 404)
    (tmp_path / "companies.yaml").write_text("- {name: Acme, ats: greenhouse, id: acme}\n"
                                             "- {name: Gone, ats: greenhouse, id: gone}\n")
    for _ in range(3):
        summary, _ = run(tmp_path, s, monkeypatch)
        assert "Gone (greenhouse)" in [x["source"] for x in summary["sources"]]
    summary, _ = run(tmp_path, s, monkeypatch)
    assert [x["source"] for x in summary["sources"]] == ["Acme (greenhouse)"]


def test_job_taken_down_by_the_company_is_marked_closed(tmp_path, monkeypatch):
    s = setup(tmp_path, days_old=0)
    run(tmp_path, s, monkeypatch)                                   # job stored
    other = Resp({"jobs": [{"id": 2, "title": "Office Manager", "absolute_url": "https://x/acme/2",
                            "location": {"name": "Berlin, Germany"}, "first_published": ISO(0), "content": "x"}]})
    s.routes["boards/acme/jobs"] = other                            # the Python job is gone
    run(tmp_path, s, monkeypatch)
    run(tmp_path, s, monkeypatch)
    d = JobDB(tmp_path / "jobs.db").jobs()[0]
    assert d["title"] == "Python Backend Engineer" and d["closed"]
    # a feed that answers with nothing at all (hiccup) closes nothing
    s2 = setup(tmp_path / "b", days_old=0) if (tmp_path / "b").mkdir() is None else None
    run(tmp_path / "b", s2, monkeypatch)
    s2.routes["boards/acme/jobs"] = Resp({"jobs": []})
    run(tmp_path / "b", s2, monkeypatch); run(tmp_path / "b", s2, monkeypatch)
    assert not JobDB(tmp_path / "b" / "jobs.db").jobs()[0].get("closed")


def test_never_had_jobs_is_information_only(tmp_path):
    from jobfinder.tracking import SourceHealth
    h = SourceHealth(tmp_path / "h.json")
    h.data = {"Empty (workable)": [0] * 9 + [-1], "Hiring (ashby)": [0] * 9 + [3],
              "Broken (lever)": [-1] * 10, "New (join)": [0, 0]}
    assert h.never_had_jobs() == ["Empty (workable)"]
    assert h.never_had_jobs(current={"Hiring (ashby)"}) == []


def test_paced_site_is_read_at_its_own_pace_and_research_reaches_its_tab(tmp_path, monkeypatch):
    """A site with min_hours_between_runs (TU München: every 3 h) is read in the first scan, skipped
    in the next one, and its research ad lands in the Research tab. India stays off."""
    import time as _t
    s = setup(tmp_path, days_old=0)
    cfg = yaml.safe_load((tmp_path / "config.yaml").read_text(encoding="utf-8"))
    assert cfg["filters"]["india"] is False and cfg["boards"]["jobspy"]["sites"] == ["linkedin"]
    cfg["boards"]["tum"].update(enabled=True, min_hours_between_runs=3)
    (tmp_path / "config.yaml").write_text(yaml.safe_dump(cfg, allow_unicode=True), encoding="utf-8")
    day = _t.strftime("%a, %d %b %Y %H:%M:%S +0000", _t.gmtime(_t.time() - 10 * 86400))      # posted 10 days ago
    s.routes["mytum.de/jobs/wissenschaftler/asRss"] = Resp(f"""<rss><channel>
      <item><title>PhD Position in Scientific Machine Learning (m/f/d)</title><link>http://portal.mytum.de/jobs/wissenschaftler/N1</link>
      <description>Python, deep learning and data science at scale. Working language is English.{FILLER}</description>
      <pubDate>{day}</pubDate></item></channel></rss>""")
    db = JobDB(tmp_path / "jobs.db")
    db.add([scan.Job(source="naukri", company="Old", title="Python Developer", url="https://n/1")], "india")
    logs = []
    monkeypatch.setattr(scan, "make_session", lambda: s)
    summary, _ = scan.run_scan(tmp_path, log=logs.append)
    assert summary["added"]["research"] == 1                       # older than 2 days, but research ads count 30 days
    assert any("Also reading this time: tum" in m for m in logs)
    assert any("removed 1 stored India jobs" in m for m in logs)
    assert not db.con.execute("SELECT 1 FROM jobs WHERE grp LIKE 'india%'").fetchone()
    asked = sum("mytum" in c[0] for c in s.calls)
    logs.clear()
    summary, _ = scan.run_scan(tmp_path, log=logs.append)
    assert sum("mytum" in c[0] for c in s.calls) == asked             # not asked again within 3 hours
    assert not any("Also reading" in m for m in logs)
    db.put("tum_last_run", (datetime.now(timezone.utc) - timedelta(hours=4)).isoformat(timespec="seconds"))
    scan.run_scan(tmp_path, log=logs.append)
    assert sum("mytum" in c[0] for c in s.calls) == asked + 1
