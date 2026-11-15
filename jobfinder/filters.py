"""Decide which jobs to show and how well each one fits.

Order of checks for every job (config.yaml holds all the settings):
  1. title    - has one of your role words, none of the excluded words
  2. location - Germany, or remote within Germany/EU
  3. age      - posted within max_age_days
  4. (fetch the full description if the source didn't send one)
  5. German   - drop jobs that ask for more German than you have (see language.py)
  6. score    - your CV skills found in the job

A job whose description could not be read even after fetching is never put in
the main list: it goes to `unchecked` (its own section on the results page).
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Callable, Optional

from . import language, places
from .models import Job

GERMAN_CITIES = [
    "berlin", "hamburg", "münchen", "munich", "muenchen", "köln", "koeln", "cologne",
    "frankfurt", "stuttgart", "düsseldorf", "duesseldorf", "dusseldorf", "leipzig",
    "dortmund", "essen", "bremen", "dresden", "hannover", "hanover", "nürnberg",
    "nuremberg", "nuernberg", "duisburg", "bochum", "wuppertal", "bielefeld", "bonn",
    "münster", "muenster", "karlsruhe", "mannheim", "augsburg", "wiesbaden", "aachen",
    "freiburg", "heidelberg", "darmstadt", "potsdam", "kiel", "mainz", "ulm", "jena",
    "regensburg", "würzburg", "wuerzburg", "ingolstadt", "erlangen", "walldorf",
    "eschborn", "leverkusen", "paderborn", "kassel", "magdeburg", "rostock", "lübeck",
    "braunschweig", "osnabrück", "oldenburg", "saarbrücken", "tübingen", "konstanz",
    "ludwigsburg", "sindelfingen", "böblingen", "leinfelden", "garching", "unterföhring",
    "neckarsulm", "heilbronn", "göttingen", "chemnitz", "halle", "erfurt", "wolfsburg",
]
GERMANY_WORDS = ["germany", "deutschland", ", de", "(de)", "dach", "bundesweit",
                 "deutschlandweit", "baden-württemberg", "baden-wuerttemberg", "bayern", "bavaria",
                 "brandenburg", "hessen", "hesse", "mecklenburg", "niedersachsen", "lower saxony",
                 "nordrhein-westfalen", "north rhine", "nrw", "rheinland-pfalz", "rhineland",
                 "saarland", "sachsen", "saxony", "schleswig-holstein", "thüringen", "thuringia"]
# A location is only rejected when it clearly names somewhere outside Germany.
# Small German towns ("Tuttlingen", "Mittweida") aren't in any list, so an
# unknown place is kept (with a note) rather than silently dropped.
FOREIGN_WORDS = [
    "united states", "usa", "u.s.", "united kingdom", "uk", "england", "scotland", "ireland", "france",
    "spain", "españa", "portugal", "italy", "italia", "netherlands", "nederland", "belgium", "luxembourg",
    "switzerland", "schweiz", "austria", "österreich", "poland", "polska", "czech", "slovakia", "hungary",
    "romania", "bulgaria", "serbia", "croatia", "slovenia", "greece", "turkey", "türkiye", "denmark",
    "sweden", "norway", "finland", "estonia", "latvia", "lithuania", "ukraine", "israel", "egypt",
    "morocco", "india", "pakistan", "china", "japan", "korea", "singapore", "australia", "canada",
    "mexico", "brazil", "argentina", "colombia", "chile", "uae", "dubai", "saudi", "south africa",
    "philippines", "vietnam", "indonesia", "malaysia", "thailand", "new zealand",
    "london", "manchester", "edinburgh", "dublin", "paris", "lyon", "amsterdam", "rotterdam", "eindhoven",
    "brussels", "madrid", "barcelona", "valencia", "lisbon", "lisboa", "porto", "milan", "milano",
    "rome", "zurich", "zürich", "geneva", "basel", "vienna", "wien", "graz", "linz", "salzburg",
    "warsaw", "warszawa", "krakow", "kraków", "wroclaw", "prague", "praha", "budapest", "bucharest",
    "sofia", "belgrade", "zagreb", "athens", "istanbul", "copenhagen", "stockholm", "oslo", "helsinki",
    "tallinn", "riga", "vilnius", "kyiv", "tel aviv", "cairo", "bangalore", "bengaluru", "hyderabad",
    "pune", "mumbai", "chennai", "new delhi", "gurgaon", "noida", "new york", "san francisco",
    "seattle", "austin", "boston", "chicago", "los angeles", "toronto", "vancouver", "montreal",
    "tokyo", "sydney", "melbourne", "são paulo", "sao paulo", "mexico city",
]
REMOTE_REGION_WORDS = ["europe", "eu", "emea", "cet", "germany", "deutschland", "dach",
                       "anywhere", "worldwide", "global"]

# German compound job titles: "Backendentwickler", "Anwendungsentwickler", "Softwareingenieur"...
GERMAN_ROLE_COMPOUND = re.compile(
    r"(software|backend|frontend|fullstack|full-stack|web|anwendungs|applikations|python|java|"
    r"ki|ai|daten|data|datenbank|cloud|devops|it|bi|plattform|platform|systems?)[- ]?"
    r"(entwickler|ingenieur|engineer)\w*")
DESCRIPTION_MIN = 200       # shorter than this counts as "no description"

DEFAULT_TECH_HINTS = ["engineer", "ingenieur", "entwickler", "developer", "scientist", "analyst",
                      "specialist", "spezialist", "consultant", "berater", "informatiker",
                      "researcher", "research", "programmer", "programmierer", "technologist"]


def _norm(s: str) -> str:
    return (s or "").lower()


def _word_in(word: str, text: str) -> bool:
    return re.search(r"(?<![a-z0-9äöüß])" + re.escape(word.lower()) + r"(?![a-z0-9äöüß])", text) is not None


def has_description(job: Job) -> bool:
    return len((job.description or "").strip()) >= DESCRIPTION_MIN


# ---------------------------------------------------------------- 1. title
def excluded_word(job: Job, cfg: dict) -> str:
    t = _norm(job.title)
    return next((w for w in cfg["exclude_title_words"] if _word_in(w, t)), "")


def has_role_word(job: Job, cfg: dict) -> bool:
    t = _norm(job.title)
    if job.academic:          # research position, already checked to be about computing/data/AI
        return True
    return any(_word_in(w, t) for w in cfg["role_keywords"]) or bool(GERMAN_ROLE_COMPOUND.search(t))


def excluded_company(job: Job, cfg: dict) -> bool:
    """A company you never want to see ("Microsoft" also drops "Microsoft Deutschland GmbH")."""
    from .tracking import norm_company
    n = norm_company(job.company or "")
    for c in cfg.get("exclude_companies") or []:
        c = norm_company(str(c))
        if c and (n == c or n.startswith(c + " ")):
            return True
    return False


def title_ok(job: Job, cfg: dict) -> bool:
    return not excluded_word(job, cfg) and has_role_word(job, cfg)


# ---------------------------------------------------------------- 2. location
INDIA_PLACES = ["india", "bharat", "bengaluru", "bangalore", "hyderabad", "mumbai", "bombay", "pune", "delhi",
                "new delhi", "delhi ncr", "ncr", "gurugram", "gurgaon", "noida", "greater noida", "chennai", "madras",
                "kolkata", "calcutta", "ahmedabad", "gandhinagar", "kochi", "cochin", "thiruvananthapuram",
                "trivandrum", "jaipur", "chandigarh", "mohali", "indore", "bhopal", "coimbatore", "nagpur",
                "bhubaneswar", "vadodara", "baroda", "surat", "mysuru", "mysore", "mangaluru", "mangalore",
                "visakhapatnam", "vizag", "lucknow", "navi mumbai", "thane", "goa", "kerala", "karnataka",
                "maharashtra", "telangana", "tamil nadu", "haryana", "uttar pradesh", "gujarat", "west bengal"]
INDIA_PRIORITY = ["bengaluru", "bangalore", "hyderabad", "mumbai", "navi mumbai", "pune"]


def india_place(job: Job) -> bool:
    if job.country in ("IN", "IND"):
        return True
    loc = _norm(job.location)
    return any(_word_in(w, loc) for w in INDIA_PLACES)


# Indeed India gives only the state ("KA, India"): the states your priority cities are in
INDIA_PRIORITY_STATES = re.compile(r"(?<![a-z])(ka|ts|tg|mh)\s*,\s*india|karnataka|telangana|maharashtra")


def india_priority(job: Job, cfg: dict) -> bool:
    loc = _norm(job.location)
    return (any(_word_in(w, loc) for w in (cfg.get("india_priority_cities") or INDIA_PRIORITY))
            or bool(INDIA_PRIORITY_STATES.search(loc)))


_DOTNET = re.compile(r"\.net\b|dot\s?net|asp\.?net|\bc#|\bc sharp|blazor|\bwpf\b", re.I)


def india_dotnet(job: Job) -> bool:
    """An India job built on .NET (and not Python): shown, but under LOW, not with the matches."""
    text = (job.title or "") + "\n" + (job.description or "")[:3000]
    return bool(_DOTNET.search(text)) and not re.search(r"\bpython\b", text, re.I)


_EXPERIENCE = re.compile(r"Years of experience:\s*(\d+)\s*(?:-|to)\s*\d+", re.I)


def min_years(job: Job) -> Optional[int]:
    """The minimum years of experience Naukri states for a job ("Years of experience: 2-5" -> 2)."""
    m = _EXPERIENCE.search(job.description or "")
    return int(m.group(1)) if m else None


def location_ok(job: Job, cfg: dict) -> bool:
    """Germany / remote-EU, or (with india: true) a place in India - marked for the India tab."""
    if _location_ok_de(job, cfg):
        job.india = False
        return True
    if cfg.get("india") and india_place(job):
        job.india = True
        return True
    return False


def _location_ok_de(job: Job, cfg: dict) -> bool:
    if job.country == "DE":
        return True
    loc = _norm(job.location)
    if job.country and job.country != "DE":
        remote_eu = (job.remote or "remote" in loc) and any(_word_in(w, loc) for w in REMOTE_REGION_WORDS)
        if not remote_eu and "germany" not in loc and "deutschland" not in loc:
            return False
    # office by office: "Berlin, CT, United States" is not Berlin; "Berlin · London" is
    verdicts = [places.office_country(o) for o in places.offices(job.location)]
    if "DE" in verdicts:
        return True
    extra = cfg.get("extra_cities", [])
    for o in places.offices(job.location):
        on = _norm(o)
        if places.office_country(o) != "foreign" and (
                any(_word_in(c, on) for c in GERMAN_CITIES + extra) or any(w in on for w in GERMANY_WORDS)):
            return True
    where = "foreign" if verdicts and all(v == "foreign" for v in verdicts) else places.country_of(job.location)
    if where == "DE":
        return True
    if where == "foreign" or any(_word_in(w, loc) for w in FOREIGN_WORDS):
        return False
    remote_text = job.remote or "remote" in loc or "home office" in loc or "homeoffice" in loc
    if remote_text and cfg.get("accept_remote_eu", True):
        if any(_word_in(w, loc) for w in REMOTE_REGION_WORDS):
            return True
        if loc.strip() in ("", "remote", "fully remote", "remote-first"):
            _flag(job, "remote - region not stated")
            return True
    if job.strict_location and not re.match(r"^\d+\s+(locations|standorte)", loc):
        return False      # worldwide board without a Germany filter: unknown place = not Germany
    if not loc.strip():
        _flag(job, "location not given")
    else:
        _flag(job, f"location '{job.location}' not recognised - check it's in Germany")
    return True


# ---------------------------------------------------------------- 3. age
def fresh_ok(job: Job, max_age_days: float) -> bool:
    if job.posted is None:
        return True          # unknown age: keep (the seen-list still hides repeats)
    days = max(max_age_days, job.max_age_days or 0)
    if job.posted >= datetime.now(timezone.utc) - timedelta(days=max_age_days):
        return True
    if job.posted >= datetime.now(timezone.utc) - timedelta(days=days):
        _flag(job, job.window_reason or "older posting: shown because this company's careers page was just added")
        return True
    return False


# ---------------------------------------------------------------- 5. German
def check_language(job: Job) -> str:
    """'required' (drop: asks for fluent German), 'foreign' (drop: ad in French, Spanish...),
    'ok', or 'unchecked' (no description to read)."""
    finding = language.assess(job.title, job.description)
    if finding.level == language.REQUIRED:
        _flag(job, f"German required: '{finding.evidence}'")
        return "required"
    lang = language.ad_language(job.title + "\n" + job.description)
    if lang and lang not in ("en", "de"):
        _flag(job, f"ad is written in {language.NAMES.get(lang, lang)}")
        return "foreign"
    if finding.level == language.MEDIUM and finding.soft:
        _flag(job, f"German preferred, not required: '{finding.evidence}'")
    elif finding.level == language.MEDIUM:
        _flag(job, f"German B2 / good level asked: '{finding.evidence}'")
    elif finding.level == language.MENTION:
        _flag(job, f"German mentioned, level not stated: '{finding.evidence}'")
    if not has_description(job):
        return "unchecked"
    if lang == "de":
        job.german_posting = True
        _flag(job, "ad written in German")
    return "ok"


def _flag(job: Job, text: str) -> None:
    if text not in job.flags:
        job.flags.append(text)


# ---------------------------------------------------------------- 6. score
# Lines (or whole sections) that list optional skills: a skill found only there counts a third.
_NICE_LINE = re.compile(
    r"von vorteil|vorteilhaft|wünschenswert|wuenschenswert|nice[- ]to[- ]have|\bbonus\b|"
    r"(?:a|an|ein|is|als|ist|are|sind)\s+(?:big\s+|great\s+|huge\s+)?plus\b|pluspunkt|\bplus:|"
    r"\bideally\b|idealerweise|\boptional\b|\bpreferred\b|advantageous|\badvantage\b|"
    r"would be great|good to have|not required|kein muss")
_HEADER = re.compile(r"^[\W\d]*[^\n.]{0,60}(:|\?)\s*$|^\s*(#+|\*\*)")
# "go" is too common a word on its own ("go-live", "on the go")
_GO = re.compile(r"\bgolang\b|\(go\)|\bgo\s*/|/\s*go\b|\bin go\b|\bgo (?:developer|engineer|backend|services?)\b")


def _lines(desc: str) -> list[tuple[str, bool]]:
    """(line, is_optional) for every line/sentence of the description."""
    out, in_nice_section = [], False
    for raw in re.split(r"\n+|(?<=[.!?])\s+(?=[A-ZÄÖÜ•\-*])", desc or ""):
        line = _norm(raw).strip()
        if not line:
            continue
        nice = bool(_NICE_LINE.search(line))
        if _HEADER.search(raw.strip()) and len(line) < 80:
            in_nice_section = nice          # a heading starts a new section
            out.append((line, nice))
            continue
        out.append((line, nice or in_nice_section))
    return out


def _skill_in(skill: str, text: str) -> bool:
    if skill == "go":
        return bool(_GO.search(text))
    # "Computer-Vision-Anwendungen" = "computer vision", "scikit learn" = "scikit-learn"
    return _word_in(skill.replace("-", " "), text.replace("-", " ").replace("­", ""))


def score(job: Job, cfg: dict) -> None:
    title = _norm(job.title)
    lines = _lines(job.description)
    core = "\n".join(l for l, nice in lines if not nice)
    optional = "\n".join(l for l, nice in lines if nice)
    pts, hits = 0, []
    for kw, weight in cfg["skills"].items():
        k = kw.lower()
        if _skill_in(k, title):
            pts += weight * 2
            hits.append(kw)
        elif _skill_in(k, core):
            pts += weight
            hits.append(kw)
        elif _skill_in(k, optional):
            pts += max(1, weight // 3)            # only "nice to have"
            hits.append(kw)

    # core technologies you don't have: count against the job
    gaps, penalty = [], 0
    for kw, weight in (cfg.get("not_my_skills") or {}).items():
        k = kw.lower()
        if _skill_in(k, title):
            penalty += weight * 2
            gaps.append(kw)
        elif _skill_in(k, core):
            penalty += weight
            gaps.append(kw)
    penalty = min(penalty, cfg.get("max_gap_penalty", 14))
    pts -= penalty
    job.gaps = gaps
    if gaps:
        _flag(job, "main stack not yours: " + ", ".join(gaps))

    for pat in cfg.get("seniority_penalty", []):
        if re.search(pat, core):
            pts -= 5
            _flag(job, "asks for many years of experience")
            break
    if job.age_hours is not None and job.age_hours <= 48:
        pts += 3                                  # fresher = better odds
    job.score, job.matched = pts, hits


# ---------------------------------------------------------------- all steps
@dataclass
class Result:
    matched: list[Job] = field(default_factory=list)      # passed everything, language checked
    maybe: list[Job] = field(default_factory=list)        # title missed, description fits
    unchecked: list[Job] = field(default_factory=list)    # passed, but no description to check German
    low: list[Job] = field(default_factory=list)          # passed every filter, but few of your skills (0..min_score)
    rejected: list[tuple[Job, str]] = field(default_factory=list)   # recent jobs dropped + reason
    stats: dict = field(default_factory=lambda: {"title": 0, "location": 0, "too_old": 0, "german": 0, "company": 0,
                                                 "low": 0, "negative": 0})


def _maybe_candidate(job: Job, cfg: dict) -> bool:
    t = _norm(job.title)
    return any(h in t for h in cfg.get("maybe_title_hints", DEFAULT_TECH_HINTS))


def _fresh_bonus(j: Job) -> int:
    return 3 if j.age_hours is not None and j.age_hours <= 48 else 0


DOMAIN_SKILLS = {"energy", "energie", "renewable", "forecasting", "time series"}


def tech_skills(matched: list[str], cfg: dict) -> list[str]:
    """Matched skills that are technologies - not domain words like energy or forecasting,
    which appear in many company introductions and say nothing about the job's tech."""
    domain = {d.lower() for d in cfg.get("domain_skills", DOMAIN_SKILLS)}
    return [m for m in matched if m.lower() not in domain]


def skill_points(j: Job, cfg: dict) -> int:
    """Skill points for a title without your role words (j must be scored): without the
    freshness bonus, which every new job gets."""
    return j.score - _fresh_bonus(j)


def classify(jobs: list[Job], cfg: dict, max_age_days: float,
             fetch_descriptions: Optional[Callable[[list[Job]], None]] = None,
             judge: Optional[Callable] = None) -> Result:
    """fetch_descriptions(jobs) is called once with the jobs that passed steps 1-3,
    so the missing descriptions can be fetched before the German check.

    A title without any of your role words no longer decides on its own: unless it's clearly
    excluded (senior, sales, intern ...), the ad is read and judged on your skills -
    judge(skill_points, title, description) -> ('inbox' | 'low' | None, similarity)."""
    from .semantic import keyword_only
    judge = judge or keyword_only
    res = Result()
    candidates: list[tuple[Job, str]] = []           # (job, "role" | "skills")

    for j in jobs:
        if not j.title or not j.url:
            continue
        if excluded_company(j, cfg):
            res.stats["company"] += 1
            continue
        fresh = fresh_ok(j, max_age_days)
        excluded = excluded_word(j, cfg)
        if excluded or not has_role_word(j, cfg):
            if not fresh:
                res.stats["title"] += 1
                continue
            if not excluded and location_ok(j, cfg):
                candidates.append((j, "skills"))     # judged on what the ad asks for
            else:
                res.stats["title"] += 1
                res.rejected.append((j, f"title contains '{excluded}'" if excluded
                                     else "title has none of your role words"))
            continue
        if not location_ok(j, cfg):
            res.stats["location"] += 1
            if fresh:
                res.rejected.append((j, f"location '{j.location or 'not given'}' not in Germany/remote-EU" + ("/India" if cfg.get("india") else "")))
            continue
        if not fresh:
            res.stats["too_old"] += 1
            continue
        candidates.append((j, "role"))

    if fetch_descriptions:
        # role-word jobs: always read. Others: the most tech-looking first, a limited number per scan
        limit = int(cfg.get("skills_fetch_limit", 300))      # safety net only: each text is read once
        need = [j for j, k in candidates if k == "role" and not has_description(j)]
        extra = sorted((j for j, k in candidates if k == "skills" and not has_description(j)),
                       key=lambda j: not _maybe_candidate(j, cfg))[:limit]
        before = {id(j): j.location for j, _ in candidates}
        fetch_descriptions(need + extra)
        # reading the detail can reveal the real location ("3 Locations" -> "Pune · Kraków"): check again
        kept = []
        for j, kind in candidates:
            if j.location != before[id(j)] and not location_ok(j, cfg):
                res.stats["location"] += 1
                res.rejected.append((j, f"location '{j.location}' not in Germany/remote-EU" + ("/India" if cfg.get("india") else "")))
            else:
                kept.append((j, kind))
        candidates = kept

    for j, kind in candidates:
        lo, cap = min_years(j), cfg.get("india_max_min_experience")
        if j.india and lo is not None and cap is not None and lo > int(cap):
            res.stats["title"] += 1
            res.rejected.append((j, f"asks for at least {lo} years of experience (more than {cap})"))
            continue
        verdict = check_language(j)
        if j.india and verdict == "foreign" and not has_description(j):
            j.flags = [f for f in j.flags if not f.startswith("ad is written in")]   # a few skill tags, not an ad text
            verdict = "unchecked"
        if j.india and verdict == "unchecked" and kind == "role":
            verdict = "ok"            # no German check needed in India; the title and skills still decide
        if verdict == "required":
            res.stats["german"] += 1
            res.rejected.append((j, next(f for f in j.flags if f.startswith("German required"))))
            continue
        if verdict == "foreign":
            res.stats["location"] += 1
            res.rejected.append((j, next(f for f in j.flags if f.startswith("ad is written in"))))
            continue
        if kind == "skills":
            if verdict == "unchecked" or not has_description(j):
                # couldn't read what it asks for: better seen and skipped than missed
                _flag(j, "title has none of your role words and the description couldn't be read - have a look yourself")
                res.stats["low"] += 1
                res.low.append(j)
                continue
            score(j, cfg)
            pts = skill_points(j, cfg)
            n_tech = len(tech_skills(j.matched, cfg))
            try:
                verdict2, sim = judge(pts, j.title, j.description, n_tech)
            except Exception:
                from .semantic import decide
                verdict2, sim = decide(pts, None, None, n_tech), None
            why = f"skill points {pts}" + (f", model similarity {sim:.2f}" if sim is not None else "")
            if verdict2 == "inbox" and j.india and india_dotnet(j):
                verdict2 = "low"
            # Your own decisions (Nov 2026): of the jobs found this way with under 8 skill points you
            # skipped 57 and applied to none - they wait in Low match instead of filling the Inbox.
            if verdict2 == "inbox" and pts < int(cfg.get("skills_inbox_min_points", 0)):
                verdict2 = "low"
                why += f" - under {int(cfg['skills_inbox_min_points'])}: Low match"
            if verdict2 == "inbox":
                _flag(j, f"found by skills - the title has none of your role words ({why})")
                if j.posted is None:
                    _flag(j, "posting date not given")
                res.matched.append(j)
            elif verdict2 == "low":
                _flag(j, f"found by skills - the title has none of your role words ({why})")
                res.stats["low"] += 1
                res.low.append(j)
            else:
                res.stats["title"] += 1
                res.rejected.append((j, f"title has none of your role words ({why})"))
            continue
        score(j, cfg)
        if j.posted is None:
            _flag(j, "posting date not given")
        if verdict == "unchecked":
            _flag(j, "description couldn't be read - check the German requirement yourself")
            if j.score >= 0:      # only the title could be scored: drop only clear misfits ("... (Go)")
                res.unchecked.append(j)
            else:
                res.rejected.append((j, f"score {j.score}: title names a stack you don't use (no description)"))
        elif j.score >= cfg.get("min_score", 0) and j.india and india_dotnet(j):
            _flag(j, ".NET job - moved to LOW")
            res.stats["low"] += 1
            res.low.append(j)
        elif j.score >= cfg.get("min_score", 0):
            res.matched.append(j)
        elif j.score >= 0:
            res.stats["low"] += 1                 # fits your filters, little skill overlap: own tab
            res.low.append(j)
        else:
            res.stats["negative"] += 1
            res.rejected.append((j, f"score {j.score}: main stack is one you don't use"))
    return res


def apply(jobs: list[Job], cfg: dict, max_age_days: float) -> tuple[list[Job], dict]:
    """Simple form: matched jobs + counts (unchecked ones are left out)."""
    res = classify(jobs, cfg, max_age_days)
    return res.matched, res.stats
