"""Berlin Startup Jobs, GermanTechJobs, the extra research sites, pacing, India off, company link first."""
import json
from datetime import datetime, timedelta, timezone

from jobfinder.db import JobDB
from jobfinder.models import Job, source_rank
from jobfinder.sources import boards as b


class R:
    def __init__(self, text="", data=None):
        self.text, self._data = text, data

    def raise_for_status(self):
        pass

    def json(self):
        return self._data


class S:
    def __init__(self, pages):
        self.pages, self.asked = pages, []

    def get(self, url, **kw):
        self.asked.append((url, kw.get("params")))
        for key, page in self.pages.items():
            if key in url:
                return page if isinstance(page, R) else R(page)
        raise RuntimeError("404 " + url)


NOW = datetime.now(timezone.utc)
RFC = NOW.strftime("%a, %d %b %Y %H:%M:%S +0000")


def test_dates_as_feeds_write_them():
    assert b.any_date("Fri, 13 Nov 2026 2:00:00 GMT") == datetime(2026, 11, 13, 2, tzinfo=timezone.utc)
    assert b.any_date("Fri, 13 Nov 2026 00:33:10 +0100") == datetime(2026, 11, 12, 23, 33, 10, tzinfo=timezone.utc)
    assert b.any_date("13.11.2026").date().isoformat() == "2026-11-13"
    assert b.any_date("10. Nov 2026").date().isoformat() == "2026-11-10"
    assert b.any_date("2026-11-10 00:00:00").date().isoformat() == "2026-11-10"
    assert b.any_date("") is None and b.any_date("soon") is None


def test_berlin_startup_jobs_title_company_and_week_window():
    feed = f"""<rss><channel><title>Jobs</title>
    <item><title>Backend Engineer (Python) // Ferto AI</title><link>https://berlinstartupjobs.com/engineering/x/</link>
      <pubDate>{RFC}</pubDate><category><![CDATA[Python]]></category><description><![CDATA[<p>teaser</p>]]></description></item>
    <item><title>Senior Data Scientist &#8211; Python &#038; ML // DATATRONiQ</title><link>https://berlinstartupjobs.com/engineering/y/</link>
      <pubDate>{RFC}</pubDate></item></channel></rss>"""
    jobs = b.berlinstartupjobs(S({"berlinstartupjobs.com/engineering/feed": feed}), {})
    assert [(j.title, j.company) for j in jobs] == [("Backend Engineer (Python)", "Ferto AI"),
                                                    ("Senior Data Scientist – Python & ML", "DATATRONiQ")]
    assert jobs[0].location == "Berlin, Germany" and jobs[0].max_age_days == 7
    assert jobs[0].description == ""            # so the full ad is read afterwards
    assert jobs[0].posted is not None


def test_germantechjobs_reads_jobs_and_drops_long_running_ads():
    def job(title, day):
        return f"""<job id="1" category="IT"><title><![CDATA[{title}]]></title>
          <link><![CDATA[https://germantechjobs.de/jobs/{title.replace(' ', '-')}]]></link><country><![CDATA[Germany]]></country>
          <city><![CDATA[Köln]]></city><salary><![CDATA[60.000 - 75.000 € per year]]></salary><company><![CDATA[Acme GmbH]]></company>
          <pubdate><![CDATA[{day}]]></pubdate><description><![CDATA[<p><b>Requirements:</b></p><ul><li>Python</li><li>FastAPI</li></ul>]]></description></job>"""
    today = NOW.strftime("%d.%m.%Y")
    feed = "<jobs>" + job("Python Entwickler", today) + job("Old Promoted Ad", "01.12.2020") + "</jobs>"
    jobs = b.germantechjobs(S({"job_feed.xml": feed}), {})
    assert [j.title for j in jobs] == ["Python Entwickler"]
    j = jobs[0]
    assert j.company == "Acme GmbH" and j.location == "Köln, Germany" and "FastAPI" in j.description
    assert j.description.startswith("Salary: 60.000")


def test_research_needs_a_real_computing_topic():
    assert b.on_topic("PhD position in Scientific Machine Learning")
    long_bio = "We study ecology. " * 120 + "Experience in data analysis is welcome."
    assert not b.on_topic("PhD position in ecology", long_bio)             # one passing mention
    assert b.on_topic("Wissenschaftliche*r Mitarbeiter*in", "Machine Learning, Python und Deep Learning " + "x " * 900)
    assert not b._research_fit("Postdoktorand (m/w/d) – Large Language Models")
    assert not b._research_fit("Softwareentwickler Python")                # not a research title


def test_fraunhofer_feed_research_to_tab_engineering_to_inbox():
    feed = f"""<rss><channel>
    <item><title><![CDATA[Wissenschaftliche*r Mitarbeiter*in - Machine Learning für Energiesysteme (Sankt Augustin, DE, 53757)]]></title>
      <description><![CDATA[<p>Deep Learning, Python</p>]]></description><link>https://jobs.fraunhofer.de/job/a/1/</link><pubDate>{RFC}</pubDate></item>
    <item><title><![CDATA[Softwareentwickler*in Python (w/m/d) (Köln)]]></title>
      <description><![CDATA[<p>FastAPI</p>]]></description><link>https://jobs.fraunhofer.de/job/b/2/</link><pubDate>{RFC}</pubDate></item>
    <item><title><![CDATA[Softwareentwickler*in Backend (w/m/d)]]></title>
      <description><![CDATA[<p>Java</p>]]></description><link>https://jobs.fraunhofer.de/job/c/3/</link><pubDate>{RFC}</pubDate></item>
    </channel></rss>"""
    s = S({"jobs.fraunhofer.de/services/rss/job": feed})
    jobs = b.fraunhofer(s, {"keywords": ["machine learning", "software"]})
    assert len(jobs) == 3 and len(s.asked) == 2                           # the same ad from two searches counts once
    assert s.asked[0][1] == {"locale": "de_DE", "keywords": "(machine learning)"}
    wm, dev, dev2 = jobs
    assert wm.academic and wm.title.endswith("Energiesysteme") and wm.location == "Sankt Augustin, Germany"
    assert wm.max_age_days == 30 and b.is_academic(wm)
    assert not dev.academic and dev.title == "Softwareentwickler*in Python (w/m/d)" and dev.location == "Köln, Germany"
    assert dev2.title == "Softwareentwickler*in Backend (w/m/d)" and dev2.location == "Germany"   # (w/m/d) is not a place
    assert dev.max_age_days is None and dev.company == "Fraunhofer-Gesellschaft"


def test_bund_opens_only_research_titles_once():
    b._BUND_SEEN.clear()
    feed = f"""<rss><channel>
    <item><title>Schulsekret&#228;rin (m/w/d)</title><link>https://www.service.bund.de/IMPORTE/1.html#track=feed-jobs</link>
      <description><![CDATA[Arbeitgeber: <strong>Kreis</strong><br /> Ort: <strong>65232 Taunusstein</strong>]]></description>
      <pubDate>{RFC}</pubDate><guid>https://www.service.bund.de/IMPORTE/1.html</guid></item>
    <item><title>Wissenschaftliche/r Mitarbeiter/in (m/w/d)</title><link>https://www.service.bund.de/IMPORTE/2.html#track=feed-jobs</link>
      <description><![CDATA[Arbeitgeber: <strong>Uni</strong><br /> Ort: <strong>50667 Köln</strong>]]></description>
      <pubDate>{RFC}</pubDate><guid>https://www.service.bund.de/IMPORTE/2.html</guid></item>
    <item><title>Wissenschaftliche/r Mitarbeiter/in Strafrecht</title><link>x</link>
      <description><![CDATA[Arbeitgeber: <strong>Uni</strong>]]></description>
      <pubDate>{RFC}</pubDate><guid>https://www.service.bund.de/IMPORTE/3.html</guid></item></channel></rss>"""
    ad = """<h1>Wis­sen­schaft­li­che/r</h1><p>Arbeitgeber: Universität zu Köln </p><dl><dt>Ort</dt><dd>Köln
      <br><a>Karte</a></dd></dl><!--Tätigkeit einfügen--><section><p>Forschung zu Machine Learning, Python und
      Deep Learning.</p></section><!--Tätigkeit ende--><!--Anforderung einfügen--><footer>Stellenangebote</footer>"""
    law = ad.replace("Machine Learning, Python und\n      Deep Learning", "Strafprozessrecht")
    s = S({"RSSGenerator_Stellen": feed, "IMPORTE/2.html": ad, "IMPORTE/3.html": law})
    jobs = b.bund(s, {})
    assert [(j.company, j.location, j.academic) for j in jobs] == [("Universität zu Köln", "Köln, Germany", True)]
    assert "Machine Learning" in jobs[0].description and "Stellenangebote" not in jobs[0].description
    assert not any("IMPORTE/1.html" in u for u, _ in s.asked)             # the school secretary ad is never opened
    n = len(s.asked)
    assert len(b.bund(s, {})) == 1 and len(s.asked) == n + 1              # second run: only the feed is read again


def test_dfki_json_skips_training_and_admin():
    def job(i, title, area, place):
        return {"id": i, "title": title[:20], "weblink": f"https://jobs.dfki.de/de/vacancy/{i}.html",
                "publication": {"internet": {"publish_date": "2026-11-10 00:00:00"}},
                "classifications": {"a": {"name": "Stellenausschreibung Titel lang - DE", "values": [{"class_val": title}]},
                                    "b": {"name": "Tätigkeitsbereich", "values": [{"class_val": area}]},
                                    "c": {"name": "Standort", "values": [{"class_val": place}]}}}
    data = {"jobs": [job(1, "Ausbildung zum Fachinformatiker", "Ausbildung", "Bremen"),
                     job(2, "Researcher (m/w/d/x) im Bereich Wissensextraktion und kausale Modellierung", "Wissenschaft", "Berlin"),
                     job(3, "Referent:in Kommunikation", "Verwaltung", "Osnabrück"),
                     job(4, "Softwareentwickler (m/w/d)", "Technik", "Kaiserslautern")]}
    jobs = b.dfki(S({"jobs.dfki.de": R(data=data)}), {})
    assert [(j.title[:10], j.academic, j.location) for j in jobs] == [("Researcher", True, "Berlin, Germany"),
                                                                      ("Softwareen", False, "Kaiserslautern, Germany")]
    assert jobs[0].posted.date().isoformat() == "2026-11-10"


def test_rwth_table_rows():
    page = """<table><tbody><tr> <td><h3><a href="?sortby=STELLENBEZ">Stelle</a></h3>
      <a href="/go/id/kbag/file/V000011696/" target="_self">Wissenschaftliche/r Mitarbeiter/in (w/m/d) - KI und Simulation [V000011696]</a><br>[veröffentlicht am 09.11.2026]</td>
      <td><h3><a href="?sortby=ANTRAGSTELLER">Hochschuleinrichtung [IKZ]</a></h3>Institut für Oberflächentechnik [419510]</td><td>30.11.2026</td></tr>
      <tr><td><a href="/go/id/kbag/file/V000011716/">Studentische Hilfskraft (w/m/d) – IT [V000011716]</a><br>[veröffentlicht am 12.11.2026]</td>
      <td>IT Center [022000]</td></tr></tbody></table>"""
    rows = b.rwth_rows(page)
    assert rows[0][0] == "https://www.rwth-aachen.de/go/id/kbag/file/V000011696/"
    assert rows[0][1].endswith("KI und Simulation") and rows[0][3] == "Institut für Oberflächentechnik"
    jobs = b.rwth(S({"rwth-aachen.de": page}), {})
    assert len(jobs) == 1 and jobs[0].academic and jobs[0].posted.date().isoformat() == "2026-11-09"


def test_tum_and_daad_feeds():
    tum = f"""<rss><channel><title>Academic staff</title>
      <item><title>PhD Position in Trustworthy AI for Medicine</title><link>http://portal.mytum.de/jobs/wissenschaftler/N1</link>
        <description>Chair for AI in Medicine</description><pubDate>{RFC}</pubDate></item>
      <item><title>Wissenschaftliche/r Mitarbeiter/in im Bereich Tissue Engineering (m/w/d)</title><link>http://portal.mytum.de/x</link>
        <description>Zellkultur</description><pubDate>{RFC}</pubDate></item></channel></rss>"""
    jobs = b.tum(S({"mytum.de": tum}), {})
    assert [j.title for j in jobs] == ["PhD Position in Trustworthy AI for Medicine"]
    assert jobs[0].url.startswith("https://") and jobs[0].academic
    daad = """<rss><channel><item><guid><![CDATA[1]]></guid><title><![CDATA[Doctoral Researcher (m/f/d) in Software Engineering for Automated Driving]]></title>
        <description><![CDATA[<p>Clausthal University of Technology invites applications. Machine learning, Python.</p>]]></description>
        <company><![CDATA[]]></company><location><![CDATA[Clausthal-Zellerfeld]]></location>
        <link>https://www.daad.de/detail/1/?phd-id=1</link><pubDate>30. Oct 2026</pubDate></item>
      <item><guid>2</guid><title><![CDATA[PhD position in ecology (m/f/d)]]></title><description><![CDATA[<p>University of Freiburg. Field work.</p>]]></description>
        <location><![CDATA[Freiburg]]></location><link>https://www.daad.de/detail/2/</link><pubDate>10. Nov 2026</pubDate></item></channel></rss>"""
    jobs = b.daad(S({"api.daad.de": daad}), {})
    assert len(jobs) == 1
    assert jobs[0].company == "Clausthal University of Technology" and jobs[0].location == "Clausthal-Zellerfeld, Germany"


def test_every_new_site_is_a_board():
    for name in ("berlinstartupjobs", "germantechjobs", "bund", "fraunhofer", "dlr", "dfki", "tum", "daad", "rwth"):
        assert name in b.BOARDS


# ------------------------------------------------------------------ database
def _job(source, url, title="Python Developer", company="Acme GmbH"):
    return Job(source=source, company=company, title=title, url=url, location="Köln, Germany", description="Python FastAPI")


def test_company_link_replaces_linkedin_link_but_keeps_status(tmp_path):
    db = JobDB(tmp_path / "jobs.db")
    li = _job("linkedin", "https://www.linkedin.com/jobs/view/1")
    assert db.add([li], "main") == 1
    db.set_status(li.key, "saved")
    own = _job("greenhouse", "https://boards.greenhouse.io/acme/jobs/7")
    assert db.add([own], "main") == 0                                     # the same job: not new again
    rows = db.con.execute("SELECT key, status, data FROM jobs").fetchall()
    assert len(rows) == 1 and rows[0]["status"] == "saved"
    d = json.loads(rows[0]["data"])
    assert d["url"] == own.url and d["source"] == "greenhouse" and d["also_on"] == li.url
    assert not db.is_new(li) and not db.is_new(own)
    # the other way round nothing changes: a LinkedIn copy never replaces the company's link
    assert db.add([_job("linkedin", "https://www.linkedin.com/jobs/view/2")], "main") == 0
    assert json.loads(db.con.execute("SELECT data FROM jobs").fetchone()["data"])["url"] == own.url
    assert source_rank("greenhouse") < source_rank("germantechjobs") < source_rank("linkedin") == source_rank("unknown site")


def test_india_off_deletes_its_stored_jobs(tmp_path):
    db = JobDB(tmp_path / "jobs.db")
    db.add([_job("naukri", "https://naukri.com/1", company="A")], "india")
    db.add([_job("naukri", "https://naukri.com/2", company="B")], "india_low")
    db.add([_job("greenhouse", "https://x/3", company="C")], "main")
    assert db.delete_groups(("india", "india_low")) == 2
    assert [r["grp"] for r in db.con.execute("SELECT grp FROM jobs")] == ["main"]
    assert db.delete_groups(("india", "india_low")) == 0


def test_skill_found_job_under_8_points_waits_in_low_match():
    import yaml
    from pathlib import Path
    from jobfinder import filters
    cfg = yaml.safe_load((Path(__file__).resolve().parent.parent / "config.yaml").read_text(encoding="utf-8"))["filters"]
    assert cfg["skills_inbox_min_points"] == 8
    text = ("Your tasks: you build data pipelines in Python with FastAPI, Docker and PostgreSQL. "
            "We work in English. " * 12)
    judge = lambda pts, title, desc, n_tech=2: ("inbox", 0.88)          # the model always says: your kind of work

    def place(points):
        j = Job(source="greenhouse", company="Acme", title="Platform Specialist", url=f"https://x/{points}",
                location="Köln, Germany", description=text, posted=NOW)
        old = filters.skill_points
        filters.skill_points = lambda job, c: points
        try:
            res = filters.classify([j], cfg, 2, judge=judge)
        finally:
            filters.skill_points = old
        return ("inbox" if j in res.matched else "low" if j in res.low else "out"), j.flags

    assert place(7)[0] == "low" and "under 8: Low match" in " ".join(place(7)[1])
    assert place(8)[0] == "inbox"
    cfg0 = dict(cfg, skills_inbox_min_points=0)
    j = Job(source="greenhouse", company="Acme", title="Platform Specialist", url="https://x/z", location="Köln, Germany",
            description=text, posted=NOW)
    old = filters.skill_points
    filters.skill_points = lambda job, c: 2
    try:
        assert j in filters.classify([j], cfg0, 2, judge=judge).matched    # 0 = rule off
    finally:
        filters.skill_points = old


def test_compound_student_and_leader_titles_are_excluded():
    import yaml
    from pathlib import Path
    from jobfinder import filters
    cfg = yaml.safe_load((Path(__file__).resolve().parent.parent / "config.yaml").read_text(encoding="utf-8"))["filters"]
    for title in ("Pflichtpraktikum: Shared Control für mobile Arbeitsmaschinen", "Studentische Hilfskraft Machine Learning",
                  "Postdoctoral Researcher - Cryptography & AI", "Team Leader Data Management (m/f/d)", "Trainee Data Engineering"):
        assert filters.excluded_word(Job(source="x", company="c", title=title, url="u"), cfg), title
    for title in ("Python Backend Engineer", "Machine Learning Engineer (m/f/d)", "Leadership-free Data Engineer"):
        assert not filters.excluded_word(Job(source="x", company="c", title=title, url="u"), cfg), title
