"""Research positions (academics.de + other boards): found, kept to computing/data/AI, own tab."""
import json
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
sys.path.insert(0, str(Path(__file__).resolve().parent))

import yaml

from jobfinder import filters, scan
from jobfinder.models import Job
from jobfinder.sources import boards
from test_jobfinder import FILLER, FakeSession, Resp

ROOT = Path(__file__).resolve().parent.parent
A = "https://www.academics.de/jobs/"


def ld(obj):
    return f'<script type="application/ld+json">{json.dumps(obj)}</script>'


def listing(items):
    return "<html>" + ld([{"@type": "WebSite"}, {"@type": "ItemList", "itemListElement": [
        {"position": i + 1, "item": {"name": t, "@id": A + slug}, "@type": "ListItem"}
        for i, (slug, t) in enumerate(items)]}]) + "</html>"


def ad(title, org, town, text, days_old=3, country="DE"):
    posted = (datetime.now(timezone.utc) - timedelta(days=days_old)).isoformat()
    return "<html>" + ld([{"@type": "Organization"}, {
        "@type": "JobPosting", "title": title, "datePosted": posted, "description": "Zum Hauptinhalt springen " + text,
        "hiringOrganization": {"name": org},
        "jobLocation": [{"address": {"addressCountry": country, "addressLocality": town}}]}]) + "</html>"


ITEMS = [
    ("wimi-ki-reutlingen-1", "Wissenschaftliche:r Mitarbeiter:in Digitalisierung und Künstliche Intelligenz (KI) (m/w/x)"),
    ("wimi-generic-2", "Wissenschaftliche Mitarbeiterin / Wissenschaftlicher Mitarbeiter (m/w/d)"),
    ("wimi-chemie-3", "Wissenschaftlicher Mitarbeiter (m/w/d) Lehrstuhl Chemie"),
    ("professur-4", "Professur (W2) für Informatik"),
    ("hilfskraft-5", "Studentische Hilfskraft Informatik"),
    ("admin-6", "Systemadministrator*in (m/w/d)"),
    ("wien-7", "University assistant predoctoral (Universitätsassistent:in) Machine Learning"),
]
ADS = {
    "wimi-ki-reutlingen-1": ad(ITEMS[0][1], "Hochschule Reutlingen", "Reutlingen",
                               "Forschung zu Machine Learning mit Python und PyTorch. Englisch, gute Deutschkenntnisse." + FILLER),
    "wimi-generic-2": ad(ITEMS[1][1], "Universität zu Köln", "Köln",
                         "Projekt zu Large Language Models (LLM) und Data Science, Python, Docker. English is fine." + FILLER),
    "wimi-chemie-3": ad(ITEMS[2][1], "Universität Bonn", "Bonn", "Synthese organischer Verbindungen im Labor." + FILLER),
    "wien-7": ad(ITEMS[6][1], "Universität Wien", "Wien", "Machine learning research, Python." + FILLER, country="AT"),
}


def session():
    routes = {"stellenanzeigen/": Resp(listing(ITEMS))}
    routes.update({A + k: Resp(v) for k, v in ADS.items()})
    return FakeSession(routes)


def test_titles():
    assert boards.academic_title(ITEMS[0][1]) and boards.academic_title(ITEMS[1][1])
    assert not boards.academic_title("Professur (W2) für Informatik")
    assert not boards.academic_title("Studentische Hilfskraft Informatik")
    assert not boards.academic_title("Postdoc in Machine Learning")
    assert boards.academic_title("Wissenschaftlicher Mitarbeiter für Doktorand oder Postdoc im Bereich KI")


def test_academics_board_reads_only_research_ads_about_computing():
    boards._ACADEMICS_SEEN.clear()
    s = session()
    jobs = boards.academics(s, {"lists": ["informatik"], "pages": 1, "days": 30})
    got = {j.title: j for j in jobs}
    assert set(got) == {ITEMS[0][1], ITEMS[1][1], ITEMS[6][1]}          # Chemie: not about computing
    j = got[ITEMS[0][1]]
    assert j.company == "Hochschule Reutlingen" and j.location == "Reutlingen, Germany" and j.academic
    assert j.description.startswith("Forschung") and j.posted and j.max_age_days == 30
    opened = [u for u, *_ in s.calls if u.startswith(A)]
    assert not any("professur" in u or "hilfskraft" in u or "admin" in u for u in opened)
    # next scan: each ad page is read once only
    s.calls.clear()
    boards.academics(s, {"lists": ["informatik"], "pages": 1})
    assert not [u for u, *_ in s.calls if u.startswith(A)]


def test_cached_jobs_are_fresh_copies():
    boards._ACADEMICS_SEEN.clear()
    j1 = boards.academics(session(), {"lists": ["informatik"], "pages": 1})[0]
    j1.flags.append("x")
    j2 = boards.academics(session(), {"lists": ["informatik"], "pages": 1})[0]
    assert j2.flags == []


def test_other_boards_research_titles():
    cfg = yaml.safe_load((ROOT / "config.yaml").read_text(encoding="utf-8"))["filters"]
    chem = Job(source="arbeitsagentur", company="Uni Bonn", title="Wissenschaftlicher Mitarbeiter Chemie", url="u")
    assert not filters.has_role_word(chem, cfg)                  # still needs a role word
    ml = Job(source="arbeitsagentur", company="Universität Köln",
             title="Wissenschaftlicher Mitarbeiter Machine Learning (m/w/d)", url="u")
    assert filters.has_role_word(ml, cfg) and boards.is_academic(ml)
    assert not boards.is_academic(Job(source="greenhouse", company="DeepL", title="Research Scientist", url="u"))
    assert not boards.is_academic(Job(source="greenhouse", company="Acme", title="Machine Learning Engineer", url="u"))


def test_scan_puts_research_jobs_in_their_own_tab(tmp_path, monkeypatch):
    boards._ACADEMICS_SEEN.clear()
    cfg = yaml.safe_load((ROOT / "config.yaml").read_text(encoding="utf-8"))
    for name, b in cfg["boards"].items():
        b["enabled"] = name == "academics"
    cfg["boards"]["academics"]["lists"] = ["informatik"]
    (tmp_path / "config.yaml").write_text(yaml.safe_dump(cfg, allow_unicode=True), encoding="utf-8")
    (tmp_path / "companies.yaml").write_text("- {name: Acme, ats: greenhouse, id: acme}\n")
    s = session()
    s.routes["boards/acme/jobs"] = Resp({"jobs": [{"id": 1, "title": "Python Backend Engineer",
                                                   "absolute_url": "https://x/acme/1", "location": {"name": "Berlin, Germany"},
                                                   "first_published": datetime.now(timezone.utc).isoformat(),
                                                   "content": "Python, FastAPI, Docker, MLflow." + FILLER}]})
    monkeypatch.setattr(scan, "make_session", lambda: s)
    summary, res = scan.run_scan(tmp_path, log=lambda m: None)
    assert summary["added"]["main"] == 1                       # the normal job: unchanged
    assert summary["added"]["research"] == 2                   # Reutlingen + Köln; Wien isn't in Germany
    from jobfinder.db import JobDB
    rows = {d["title"]: d for d in JobDB(tmp_path / "jobs.db").jobs()}
    assert rows[ITEMS[0][1]]["group"] == "research" and rows["Python Backend Engineer"]["group"] == "main"
