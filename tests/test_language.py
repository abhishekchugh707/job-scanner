"""German-requirement detection, checked against real phrasings from job ads.

Run:  python tests/test_language.py
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from jobfinder.language import MEDIUM, MENTION, OK, REQUIRED, assess  # noqa: E402

CASES = [
    # --- "preferred" / "ideally": a wish, never a reason to drop (1 Nov 2026) ---
    ("C1-proficiency in German is highly preferred to lead client workshops.", MEDIUM),
    ("Ideally C1 German", MEDIUM),
    ("You speak English fluently; German (C1) would be nice", MEDIUM),
    ("Deutsch C1 bevorzugt", MEDIUM),
    ("Fluent German and English required, Spanish preferred", REQUIRED),
    ("Fließende Deutschkenntnisse zwingend erforderlich, idealerweise auch Französisch", REQUIRED),
    # --- the three that slipped through on 29 Oct 2026 (real text) ---
    ("rungen zu verstehen und die richtigen Rückfragen zu stellen Fließendes Deutsch (C1/C2) und "
     "konversationssichere Englischkenntnisse", REQUIRED),
    ("* **Sprachen:** Deutsch und Englisch fließend", REQUIRED),
    ("Tutor:in oder Mentor:in ist ein Plus. Du sprichst Deutsch (C1) und Englisch (B2).", REQUIRED),
    # --- hidden in a Bundesagentur job that had no description in the search ---
    ("Gute Deutschkenntnisse (ab C1-Niveau) sowie Englischkenntnisse", REQUIRED),

    # --- German ads: required ---
    ("Sehr gute Deutsch- und Englischkenntnisse in Wort und Schrift", REQUIRED),
    ("Verhandlungssichere Deutschkenntnisse", REQUIRED),
    ("Du sprichst fließend Deutsch und Englisch", REQUIRED),
    ("Deutsch auf Muttersprachniveau", REQUIRED),
    ("Deutschkenntnisse (mind. C1)", REQUIRED),
    ("Exzellente Deutschkenntnisse sind Voraussetzung", REQUIRED),
    ("Sprachen: Deutsch (verhandlungssicher), Englisch (gut)", REQUIRED),
    ("Hervorragende Kommunikationsfähigkeit auf Deutsch", REQUIRED),
    ("Deutsch in Wort und Schrift", REQUIRED),
    ("Deutschkenntnisse zwingend erforderlich", REQUIRED),
    ("Sehr gute Englisch- und Deutschkenntnisse runden dein Profil ab", REQUIRED),
    ("Deutsch-Muttersprachler oder vergleichbares Niveau", REQUIRED),
    ("Deutsch C2, Englisch B2", REQUIRED),
    ("- Du kommunizierst sicher auf Englisch und Deutsch", REQUIRED),        # VITRONIC, 29 Oct
    ("Du sprichst sicher Deutsch und Englisch", REQUIRED),
    ("You communicate confidently in German and English", REQUIRED),

    # --- English ads: required ---
    ("You are fluent in German and English", REQUIRED),
    ("Fluent German and English (written and spoken)", REQUIRED),
    ("Fluent English and German", REQUIRED),
    ("Business fluent German is required", REQUIRED),
    ("Native-level German speaker", REQUIRED),
    ("Excellent communication skills in German and English", REQUIRED),
    ("German language skills: C1 or higher", REQUIRED),
    ("C1-level German", REQUIRED),
    ("German at C1 level", REQUIRED),
    ("Proficiency in German (min. C1) is a must", REQUIRED),
    ("German is a must", REQUIRED),
    ("German is mandatory for this role", REQUIRED),
    ("Very good command of German", REQUIRED),
    ("Languages: German (fluent), English (B2)", REQUIRED),

    # --- medium: kept, flagged ---
    ("Gute Deutschkenntnisse", MEDIUM),
    ("German B2 or higher", MEDIUM),
    ("Deutsch mindestens B2", MEDIUM),
    ("Deutsch (B2/C1)", MEDIUM),
    ("Good German skills", MEDIUM),
    ("Conversational German", MEDIUM),

    # --- level not stated: kept, flagged ---
    ("Sprachen: Deutsch und Englisch", MENTION),
    ("You speak German and English", MENTION),
    ("Deutschsprachiges Team", MENTION),

    # --- fine for you ---
    ("Fluent English required, German is a plus", OK),
    ("Fluent English; German is nice to have", OK),
    ("Fließend Englisch, Deutschkenntnisse von Vorteil", OK),
    ("Fließendes Deutsch wünschenswert, aber kein Muss", OK),
    ("No German required - our working language is English", OK),
    ("German is not required", OK),
    ("Kein Deutsch erforderlich", OK),
    ("Fluent English and basic German", OK),
    ("Englisch (C1) und Deutsch (B1)", OK),
    ("German B1 is enough", OK),
    ("Grundkenntnisse in Deutsch", OK),
    ("Fluent English (German would be a big plus)", OK),
    ("Excellent knowledge of the German market", OK),
    ("Fluent English. Experience with German customers is a bonus.", OK),
    ("We are building cloud-native tools for the German Mittelstand", OK),
    ("Arbeitsort: Deutschland, Remote möglich", OK),
    ("Wir sind die Deutsche Bahn", OK),
    ("Sehr gute Python-Kenntnisse. Du arbeitest in einem internationalen Team.", OK),
    ("Fluent English is required.", OK),
    ("Sehr gute Kenntnisse in Python und Docker", OK),
    ("Unser Team spricht Englisch, Deutschkenntnisse sind ein Plus", OK),
    # --- real false alarms from 29 Oct 2026 ---
    ("A subsidy for German language courses to help you immerse yourself in the local culture", OK),
    ("~60 hours/month. We are an independent German digital marketing agency with proprietary technology", OK),
    ("We offer free German classes", OK),
    ("Languages: English, German", MENTION),
    ("Fließendes Deutsch plus gute Englischkenntnisse", REQUIRED),
    ("Our cloud-native platform serves customers; fluent English required", OK),
]

TITLE_CASES = [
    ("Junior CS Agent – German Speaking (m/f/d)", REQUIRED),
    ("Python Developer (Deutsch)", REQUIRED),
    ("German Market Analyst", OK),
    ("Backend Engineer Python", OK),
]


def test_descriptions():
    bad = []
    for text, want in CASES:
        got = assess("Software Engineer", text)
        if got.level != want:
            bad.append(f"  want {want} got {got.level}: {text!r}  [{got.evidence}]")
    assert not bad, "\n" + "\n".join(bad)


def test_titles():
    for title, want in TITLE_CASES:
        assert assess(title, "").level == want, title


def test_bullet_points_stay_separate():
    """Two bullet points must not merge: the 'plus' belongs to Docker, not to German."""
    from jobfinder.util import html_to_text
    jd = html_to_text("<ul><li>Very good German</li><li>Docker is a plus</li></ul>")
    assert jd == "• Very good German\n• Docker is a plus"
    assert assess("Dev", jd).level == REQUIRED
    jd = html_to_text("<p>Sehr gute Deutschkenntnisse</p><p>Kubernetes von Vorteil</p>")
    assert assess("Dev", jd).level == REQUIRED


def test_long_real_posting():
    jd = """**Unternehmensbeschreibung**

Wir sind als eine der führenden europäischen Management- und Technologieberatungen in Deutschland tätig.

Stellenbeschreibung

Entwicklung robuster Backend-Komponenten für datenintensive KI-Anwendungen
Design von REST-APIs mit FastAPI

Qualifikationen

Abgeschlossenes Studium der Informatik
Sehr gute Kenntnisse in Python
Gute Deutschkenntnisse (ab C1-Niveau) sowie Englischkenntnisse

Zusätzliche Informationen

Work-Life-Balance: hybrides Arbeiten innerhalb von Deutschland, Bezuschussung des Deutschlandtickets"""
    f = assess("AI Backend Developer (m/w/d)", jd)
    assert f.level == REQUIRED and "c1" in f.evidence


if __name__ == "__main__":
    fails = 0
    for name, fn in list(globals().items()):
        if name.startswith("test_"):
            try:
                fn()
                print("ok  ", name)
            except AssertionError as e:
                fails += 1
                print("FAIL", name, e)
    print(f"{len(CASES) + len(TITLE_CASES) + 1} phrasings checked")
    sys.exit(1 if fails else 0)
