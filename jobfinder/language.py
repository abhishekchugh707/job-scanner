"""Does a job posting ask for more German than you have?

The text is split into sentences and clauses. For every place where German is
mentioned as a language (not "German market", "Deutschland", ...), the level
words closest to that mention decide:

  REQUIRED  C1/C2, fluent, fließend, verhandlungssicher, native, sehr gut,
            excellent, business fluent, "in Wort und Schrift", "German required"
            -> the job is dropped
  MEDIUM    B2, gut/good, conversational, solid      -> kept, flagged
  MENTION   German named with no level ("Deutsch und Englisch") -> kept, flagged
  OK        basic / A1-B1, or "a plus", "von Vorteil", "not required"

An explicit CEFR level (A1-C2) next to the mention beats adjectives, so
"Gute Deutschkenntnisse (ab C1-Niveau)" counts as C1.
"""
from __future__ import annotations

import re
from dataclasses import dataclass

OK, MENTION, MEDIUM, REQUIRED = 0, 1, 2, 3
LABEL = {OK: "ok", MENTION: "German mentioned (level not stated)", MEDIUM: "German B2 / good level asked",
         REQUIRED: "German required"}

WINDOW = 45          # max characters between the German mention and a level word

# A language mention. "deutsch" must end the word, so "Deutschland", "deutsche Bahn"
# don't count; "Deutsch-" in "Deutsch- und Englischkenntnisse" does.
_GERMAN = re.compile(
    r"\b(deutsch(?:kenntnisse|kenntnissen|sprachig\w*)?|deutschen sprache|deutsche sprache|"
    r"deutscher sprache|german)\b")
# Words that make "German ..." a market/company/legal term rather than a language.
_NOT_LANGUAGE_NEXT = re.compile(
    r"^[\s-]*(market|markets|customers?|clients?|law|legal|tax|companies|company|government|public|"
    r"standards?|regulations?|regulatory|authorities|banks?|banking|energy|healthcare|mittelstand|"
    r"startups?|offices?|headquarters|subsidiary|entity|engineering|cities|citizens?|citizenship|"
    r"residence|work permit|residency|economy|industry|automotive|insurance|retail|"
    r"bundesliga|federal|state|universities|university|research|speaking market|"
    r"markt|kunden|recht|steuer\w*|unternehmen|behörden|banken|wirtschaft)\b")

_NICE = re.compile(
    r"\b((?:a|an|ein|is|als|ist|are|sind)\s+(?:big\s+|great\s+|huge\s+|strong\s+|definite\s+|großes\s+|klares\s+)?plus|"
    r"pluspunkt|von vorteil|vorteilhaft|wünschenswert|wuenschenswert|nice[- ]to[- ]have|bonus|"
    r"advantage\w*|beneficial|helpful|hilfreich|optional|"
    r"not (?:required|necessary|needed|a must|mandatory)|nicht (?:erforderlich|notwendig|zwingend|nötig)|"
    r"kein muss|no german|keine deutschkenntnisse|kein deutsch|without german|ohne deutsch|"
    # learning German offered as a benefit
    r"courses?|classes|lessons|tuition|learn\w*|lernen|sprachkurs\w*|deutschkurs\w*|kurse?)\b")

# For the English word "German" (also an adjective: "a German company") the sentence must
# be about language before a mention without a level counts.
_LANGUAGE_CUE = re.compile(
    r"\b(speak\w*|spoken|language\w*|skills?|knowledge|proficien\w*|fluent\w*|fluency|written|verbal|"
    r"communicat\w*|level|cefr|[abc][12]|sprache\w*|sprichst|sprechen|"
    r"required|mandatory|a must|must|essential)\b")

_REQUIRED_WORDS = re.compile(
    r"\b(required|requirement|a must|must|mandatory|essential|erforderlich|vorausgesetzt|"
    r"voraussetzung|zwingend\w*|unabdingbar|unerlässlich|notwendig|setzen wir voraus|muss)\b")

_CEFR = re.compile(r"\b([abc][12])\b\+?")
_CEFR_LEVEL = {"a1": OK, "a2": OK, "b1": OK, "b2": MEDIUM, "c1": REQUIRED, "c2": REQUIRED}

# Adjectives / phrases, strongest first (spans are masked so "sehr gute" isn't also "gute").
_STRONG = re.compile(
    r"flie(?:ß|ss)end\w*|verhandlungssicher\w*|muttersprach\w*|mother[- ]tongue|(?<![\w-])native\b(?![- ](?:app|cloud|ai))|"
    r"fluent\w*|fluency|sehr gut\w*|exzellent\w*|hervorragend\w*|ausgezeichnet\w*|perfekt\w*|"
    r"einwandfrei\w*|excellent|very good|outstanding|perfect|business[- ]?(?:fluent|level|proficien\w*)|"
    r"(?:full|professional)(?: working)? proficiency|proficien\w*|advanced|strong command|"
    r"high level|auf hohem niveau|in wort und schrift|stilsicher\w*|exceptional|superb|"
    # "Du kommunizierst sicher auf Deutsch", "communicate confidently in German"
    r"kommuni\w*\s+(?:du\s+)?(?:sehr\s+)?sicher\w*|sicher\w*\s+kommuni\w*|"
    r"(?:sprichst|sprechen|sprachlich)\s+(?:du\s+)?sicher\w*|"
    r"communicat\w*\s+confident\w*|confident\w*\s+communicat\w*|confident in")
_MEDIUM = re.compile(r"\b(gut\w*|good|solid\w*|solide\w*|sicher\w*|konversationssicher\w*|conversational|"
                     r"working knowledge|intermediate|fundiert\w*)\b")
_WEAK = re.compile(r"\b(grundkenntniss\w*|grundlegend\w*|basic\w*|basis\w*|erste kenntniss\w*|elementary|"
                   r"beginner\w*|some)\b")

_SENTENCE = re.compile(r"[\n;•·|!?]+|\.(?:\s|$)|\s\*\s|\s-\s(?=[a-zäöü])")
_CLAUSE = re.compile(r",|\s–\s|\s—\s")


# ----------------------------------------------------------- language of the ad
_STOPWORDS = {
    "en": "the and you with for our are your will this that have from team we to of in is".split(),
    "de": "und die der das mit für wir sie ihre unser bei eine ist du dich dein zu von im auf".split(),
    "fr": "et les des vous nous une pour dans avec est sur votre être notre aux du au qui ou".split(),
    "es": "y los las con para una del por que nuestro tu tus como más sus somos es en el".split(),
    "it": "e il gli con per una della sono nostro che nel alla delle questo tuo siamo".split(),
    "nl": "en de het een voor met van je jouw wij zijn ons onze bij als wordt".split(),
    "pl": "i w z na się do dla jest nasz oraz jako będziesz który są".split(),
    "pt": "e os as com para uma do da que nossa você em são mais seu".split(),
}
NAMES = {"en": "English", "de": "German", "fr": "French", "es": "Spanish", "it": "Italian",
         "nl": "Dutch", "pl": "Polish", "pt": "Portuguese"}


def ad_language(text: str) -> str:
    """Main language of a job ad ('en', 'de', 'fr', ...); '' when too short to tell."""
    words = re.findall(r"[a-zäöüßàâçéèêëîïôûùüÿñæœáíóúąćęłńśźż]+", (text or "").lower())
    if len(words) < 40:
        return ""
    counts = {lang: 0 for lang in _STOPWORDS}
    sets = {lang: set(ws) for lang, ws in _STOPWORDS.items()}
    for w in words:
        for lang, s in sets.items():
            if w in s:
                counts[lang] += 1
    best = max(counts, key=counts.get)
    ranked = sorted(counts.values(), reverse=True)
    if ranked[0] < 8 or ranked[0] < 1.3 * ranked[1]:
        # mixed: if English or German is strong too, treat it as readable
        return "en" if counts["en"] >= counts["de"] else "de"
    return best


@dataclass
class Finding:
    level: int
    evidence: str = ""
    soft: bool = False          # "preferred" / "ideally": a wish, not a requirement


def _tokens(clause: str) -> list[tuple[int, int, int, bool]]:
    """(start, end, level, is_cefr) for every level word in the clause."""
    out, masked = [], list(clause)
    for m in _CEFR.finditer(clause):
        out.append((m.start(), m.end(), _CEFR_LEVEL[m.group(1)], True))
    for rx, lvl in ((_STRONG, REQUIRED), (_MEDIUM, MEDIUM), (_WEAK, OK)):
        text = "".join(masked)
        for m in rx.finditer(text):
            out.append((m.start(), m.end(), lvl, False))
            masked[m.start():m.end()] = " " * (m.end() - m.start())
    return out


def _distance(a: tuple[int, int], b: tuple[int, int]) -> int:
    if a[1] <= b[0]:
        return b[0] - a[1]
    if b[1] <= a[0]:
        return a[0] - b[1]
    return 0


# "preferred", "ideally": a wish, not a requirement -> kept, flagged (never dropped)
_SOFT = re.compile(r"\b(preferred|preferably|ideally|idealerweise|idealerw|would be (?:nice|great|good|a plus)|"
                   r"desirable|desired|wäre schön|gern gesehen|bevorzugt)\b")


def _judge_mention(clause: str, span: tuple[int, int]) -> int:
    if _NICE.search(clause):
        return OK
    if _soft(clause, span):
        return min(MEDIUM, _judge_strict(clause, span))
    return _judge_strict(clause, span)


def _soft(clause: str, span: tuple[int, int]) -> bool:
    """A wish word right next to this mention, and no "required"/"must" next to it."""
    near = lambda rx: any(_distance(span, m.span()) <= WINDOW for m in rx.finditer(clause))
    return near(_SOFT) and not near(_REQUIRED_WORDS)


def _judge_strict(clause: str, span: tuple[int, int]) -> int:
    near = [(s, e, lvl, cefr) for s, e, lvl, cefr in _tokens(clause)
            if _distance(span, (s, e)) <= WINDOW]
    pool = [t for t in near if t[3]] or near           # an explicit A1-C2 level beats adjectives
    if pool:
        pool.sort()
        i = min(range(len(pool)), key=lambda k: _distance(span, pool[k][:2]))
        # the nearest level word, plus words chained right next to it ("B2/C1", "C1/C2")
        lo = hi = i
        while lo > 0 and pool[lo][0] - pool[lo - 1][1] <= 3:
            lo -= 1
        while hi < len(pool) - 1 and pool[hi + 1][0] - pool[hi][1] <= 3:
            hi += 1
        return min(t[2] for t in pool[lo:hi + 1])      # "B2/C1" -> B2
    if _REQUIRED_WORDS.search(clause):
        return REQUIRED
    return MENTION


def _has_level_word(clause: str, span: tuple[int, int]) -> bool:
    return any(_distance(span, (s, e)) <= WINDOW for s, e, _l, _c in _tokens(clause))


def _mentions(text: str):
    for m in _GERMAN.finditer(text):
        if _NOT_LANGUAGE_NEXT.match(text[m.end():m.end() + 30]):
            continue
        yield m


def normalize(text: str) -> str:
    t = (text or "").lower().replace("**", " ").replace("__", " ")
    t = re.sub(r"[“”„\"']", " ", t)
    # abbreviations whose dot must not end a sentence ("mind. C1", "z.B.", "e.g.")
    t = re.sub(r"\b(mind|min|bzw|ca|inkl|evtl|vgl|sog|etc|usw|ggf|bspw|max|idealerw|vorzugsw|"
               r"z\.\s?b|e\.\s?g|i\.\s?e|u\.\s?a|d\.\s?h)\.", lambda m: m.group(1).replace(".", ""), t)
    return t


def assess(title: str, description: str) -> Finding:
    """Highest German requirement found in the posting."""
    best = Finding(OK)
    t_title = normalize(title)
    for m in _mentions(t_title):
        # "German speaking", "(German)" in a job title: the job is in German
        if not _NICE.search(t_title):
            return Finding(REQUIRED, title.strip())
    text = normalize(description)
    for sentence in _SENTENCE.split(text):
        if not _GERMAN.search(sentence):
            continue
        about_language = bool(_LANGUAGE_CUE.search(sentence))
        for clause in _CLAUSE.split(sentence):
            for m in _mentions(clause):
                lvl = _judge_mention(clause, m.span())
                if m.group(1) == "german" and lvl in (MENTION, REQUIRED) and not about_language \
                        and not _has_level_word(clause, m.span()):
                    continue          # "an independent German agency", "German customers"
                if lvl > best.level:
                    best = Finding(lvl, " ".join(clause.split())[:140], _soft(clause, m.span()))
                    if lvl == REQUIRED:
                        return best
    return best
