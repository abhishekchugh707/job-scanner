"""Is a job location in Germany? Uses the GeoNames place list in data/places.json.gz:
every German place with 500+ inhabitants (37k names incl. spellings like Köln/Cologne)
and foreign cities with 15k+ inhabitants plus country names.

A German match always wins ("Berlin · London" is fine). Data: GeoNames, CC BY 4.0.
"""
from __future__ import annotations

import gzip
import json
import re
from functools import lru_cache
from pathlib import Path
from typing import Optional

DATA = Path(__file__).resolve().parent / "data" / "places.json.gz"

# words in location strings that are never place names
NOISE = {"remote", "hybrid", "office", "area", "region", "metro", "metropolitan", "greater", "europe",
         "emea", "eu", "worldwide", "anywhere", "global", "full", "time", "home", "homeoffice",
         "onsite", "on site", "standort", "standorte", "locations", "location", "hq", "headquarters",
         "and", "und", "or", "oder", "the", "of", "flexible", "mobile", "germany", "deutschland",
         "main", "mitte", "north", "south", "east", "west", "central", "center", "centre", "new", "city"}


@lru_cache(maxsize=1)
def _sets() -> tuple[dict, frozenset]:
    """German names -> [lat, lon], and foreign names."""
    with gzip.open(DATA, "rt", encoding="utf-8") as f:
        d = json.load(f)
    return d["de"], frozenset(d["foreign"])


def _norm(s: str) -> str:
    return " ".join(re.sub(r"[^\wäöüß' -]", " ", s.lower()).replace("-", " ").split())


def _grams(part: str):
    """The whole part, then 3-, 2- and 1-word pieces of it."""
    words = part.split()
    yield part
    for n in (3, 2, 1):
        for i in range(len(words) - n + 1):
            g = " ".join(words[i:i + n])
            if n > 1 or g not in NOISE:
                yield g


def _scan(location: str):
    """(first German place found as (lat, lon) or None, any foreign place found)."""
    de, foreign = _sets()
    found_foreign = False
    for raw in re.split(r"[,·/|;()\[\]•]|\s[-–]\s", location or ""):
        part = _norm(raw)
        if not part or part in NOISE:
            continue
        for g in _grams(part):
            if len(g) < 3:
                continue
            if g in de:
                return tuple(de[g]), found_foreign
            if g in foreign:
                found_foreign = True
    return None, found_foreign


# US states (minus "DE" = Delaware, which collides with "Berlin, DE") and other country markers.
# "Berlin, CT", "Hamburg, NY", "Heidelberg, PA" are US towns, not German ones.
US_STATES = {
    "al", "ak", "az", "ar", "ca", "co", "ct", "fl", "ga", "hi", "id", "il", "ia", "ks", "ky", "la", "md",
    "ma", "mi", "mn", "ms", "mo", "mt", "ne", "nv", "nh", "nj", "nm", "ny", "nc", "nd", "oh", "ok", "or",
    "pa", "ri", "sc", "sd", "tn", "tx", "ut", "vt", "va", "wa", "wv", "wi", "wy", "dc",
    "alabama", "alaska", "arizona", "arkansas", "california", "colorado", "connecticut", "delaware",
    "florida", "georgia", "hawaii", "idaho", "illinois", "indiana", "iowa", "kansas", "kentucky",
    "louisiana", "maine", "maryland", "massachusetts", "michigan", "minnesota", "mississippi",
    "missouri", "montana", "nebraska", "nevada", "new hampshire", "new jersey", "new mexico", "new york",
    "north carolina", "north dakota", "ohio", "oklahoma", "oregon", "pennsylvania", "rhode island",
    "south carolina", "south dakota", "tennessee", "texas", "utah", "vermont", "virginia", "washington",
    "west virginia", "wisconsin", "wyoming", "district of columbia",
    "ontario", "quebec", "british columbia", "alberta"}
FOREIGN_MARKERS = {"us", "usa", "u s", "u s a", "united states", "united states of america", "america", "uk",
                   "united kingdom", "great britain", "canada", "india", "latam", "apac", "north america",
                   "americas", "any state", "south america", "latin america", "central america", "lat am", "ksa", "uae", "gcc", "asia", "africa", "middle east", "mena",
                   "rom", "rou", "pol", "ind", "esp", "fra", "ita", "nld", "gbr", "irl", "prt", "cze", "hun",
                   "bgr", "hrv", "srb", "grc", "tur", "bra", "mex", "arg", "chn", "jpn", "kor", "sgp", "aus"}
COUNTRIES = {
    "afghanistan", "albania", "algeria", "argentina", "armenia", "australia", "austria", "azerbaijan",
    "bahrain", "bangladesh", "belarus", "belgium", "bolivia", "bosnia", "bosnia and herzegovina", "brazil",
    "bulgaria", "cambodia", "cameroon", "chile", "china", "colombia", "costa rica", "croatia", "cyprus",
    "czech republic", "czechia", "denmark", "dominican republic", "ecuador", "egypt", "el salvador",
    "estonia", "ethiopia", "finland", "france", "georgia", "ghana", "greece", "guatemala", "honduras",
    "hong kong", "hungary", "iceland", "india", "indonesia", "iran", "iraq", "ireland", "israel", "italy",
    "jamaica", "japan", "jordan", "kazakhstan", "kenya", "kosovo", "kuwait", "latvia", "lebanon",
    "lithuania", "luxembourg", "malaysia", "malta", "mexico", "moldova", "montenegro", "morocco",
    "nepal", "netherlands", "the netherlands", "new zealand", "nicaragua", "nigeria", "north macedonia",
    "norway", "oman", "pakistan", "panama", "paraguay", "peru", "philippines", "poland", "portugal",
    "puerto rico", "qatar", "romania", "russia", "rwanda", "saudi arabia", "senegal", "serbia",
    "singapore", "slovakia", "slovenia", "south africa", "south korea", "korea", "spain", "sri lanka",
    "sweden", "switzerland", "taiwan", "tanzania", "thailand", "tunisia", "turkey", "turkiye", "türkiye",
    "uganda", "ukraine", "united arab emirates", "uae", "uruguay", "uzbekistan", "venezuela", "vietnam",
    "zambia", "zimbabwe", "england", "scotland", "wales", "northern ireland",
    # German names
    "frankreich", "italien", "spanien", "niederlande", "belgien", "österreich", "schweiz", "polen",
    "tschechien", "ungarn", "rumänien", "bulgarien", "kroatien", "serbien", "griechenland", "türkei",
    "dänemark", "schweden", "norwegen", "finnland", "irland", "portugal", "großbritannien", "vereinigtes königreich",
    "vereinigte staaten", "indien", "china", "japan", "brasilien", "mexiko", "kanada", "russland", "ukraine"}
GERMANY = {"germany", "deutschland", "de", "deu", "ger", "allemagne", "alemania", "germania"}
# German cities big enough to win against a foreign name in the same location ("Berlin, London")
MAJOR = {"berlin", "hamburg", "münchen", "munich", "muenchen", "köln", "koeln", "cologne", "frankfurt",
         "frankfurt am main", "stuttgart", "düsseldorf", "duesseldorf", "dusseldorf", "leipzig", "dortmund",
         "essen", "bremen", "dresden", "hannover", "nürnberg", "nuremberg", "nuernberg", "duisburg", "bochum",
         "wuppertal", "bielefeld", "bonn", "münster", "muenster", "karlsruhe", "mannheim", "augsburg",
         "wiesbaden", "aachen", "freiburg", "heidelberg", "darmstadt", "potsdam", "kiel", "mainz", "ulm",
         "regensburg", "würzburg", "wuerzburg", "ingolstadt", "erlangen", "walldorf", "eschborn", "leverkusen",
         "kassel", "magdeburg", "rostock", "lübeck", "braunschweig", "osnabrück", "oldenburg", "saarbrücken",
         "chemnitz", "erfurt", "wolfsburg", "göttingen", "heilbronn", "jena"}


def _ascii(s: str) -> str:
    import unicodedata
    return "".join(c for c in unicodedata.normalize("NFKD", s) if not unicodedata.combining(c))


@lru_cache(maxsize=1)
def _foreign_ascii() -> frozenset:
    return frozenset(_ascii(n) for n in _sets()[1])


def _office_parts(office: str) -> list[str]:
    return [_norm(p) for p in re.split(r"[,()\[\]]|\s[-–]\s", office) if _norm(p)]


def offices(location: str) -> list[str]:
    """'Berlin, Germany; Austin, TX' -> ['Berlin, Germany', 'Austin, TX']"""
    return [o for o in re.split(r"[;·|/•]|\s+or\s+|\s+oder\s+|\s+&\s+|\s+and\s+", location or "") if o.strip()]


def office_country(office: str) -> Optional[str]:
    """'DE', 'foreign' or None for one office ("City, Region, Country").
    - names Germany                               -> DE
    - names a foreign country / US state / "US-…" -> foreign ("Berlin, CT", "Krakow, Poland", "US-MD-Hanover")
    - a German place and a foreign city together  -> DE only for a big German city ("Berlin, London"),
      otherwise foreign ("Pune, Gera Commerzone": Gera is a small German town, Pune is in India)
    - a German name that is also a foreign city's ("Krakow" = Krakow am See / Kraków) -> foreign
      unless it's a big German city"""
    parts = _office_parts(office)
    if not parts:
        return None
    # "Germany" / "Deutschland" written out is decisive; a bare code "DE" is not: Wilmington, DE
    # is Delaware. A code counts only when no foreign place is named next to it.
    if any(p in ("germany", "deutschland", "allemagne", "alemania", "germania") or p.endswith(" germany")
           or p.endswith(" deutschland") or p.startswith("germany ") for p in parts):
        return "DE"
    code_de = any(p in ("de", "deu", "ger") for p in parts)
    parts = [p for p in parts if p not in ("de", "deu", "ger")]
    for p in parts:
        words = p.split()
        if p in US_STATES or p in FOREIGN_MARKERS or p in COUNTRIES or (len(words) > 1 and words[0] in ("us", "usa")) \
                or any(w in COUNTRIES for w in words if len(w) > 3):
            return "foreign"
    de, foreign = _sets()
    fa = _foreign_ascii()
    de_hit = major = foreign_hit = False
    for p in parts:
        for g in _grams(p):
            if len(g) < 3:
                continue
            if g in de:
                if g in MAJOR:
                    major = True
                    de_hit = True
                elif _ascii(g) in fa:
                    foreign_hit = True               # ambiguous small German name: Krakow, Rom, Peru...
                else:
                    de_hit = True
                break
            if g in foreign or _ascii(g) in fa:
                foreign_hit = True
                break
    if de_hit and (major or not foreign_hit):
        return "DE"
    if foreign_hit:
        return "foreign"
    return "DE" if code_de else None


def country_of(location: str) -> Optional[str]:
    """'DE' if any office is in Germany, 'foreign' if all named offices are abroad, else None."""
    if not location:
        return None
    verdicts = [office_country(o) for o in offices(location)]
    if "DE" in verdicts:
        return "DE"
    return "foreign" if "foreign" in verdicts else None


def distance_km(location: str, home: tuple[float, float]) -> Optional[int]:
    """Distance from home to the nearest German place named in the location."""
    best = None
    for raw in offices(location):                        # multi-office: take the closest
        if office_country(raw) != "DE":
            continue
        coords, _ = _scan(raw)
        if coords:
            d = _haversine(home, coords)
            best = d if best is None else min(best, d)
    return None if best is None else int(round(best))


def _haversine(a: tuple[float, float], b: tuple[float, float]) -> float:
    from math import asin, cos, radians, sin, sqrt
    lat1, lon1, lat2, lon2 = map(radians, (a[0], a[1], b[0], b[1]))
    h = sin((lat2 - lat1) / 2) ** 2 + cos(lat1) * cos(lat2) * sin((lon2 - lon1) / 2) ** 2
    return 2 * 6371 * asin(sqrt(h))
