"""Add companies by pasting their careers page URL.

    python detect_ats.py "Enpal" https://www.enpal.de/karriere
    python detect_ats.py --file careers_urls.txt      # one "Name, URL" per line

Finds which hiring platform the page uses and appends the company to
companies.yaml. Pages on Workday, SuccessFactors or a custom system are
reported as 'not detected' - check those by hand or skip them.
"""
from __future__ import annotations

import argparse
from pathlib import Path

import yaml

from jobfinder.detect import detect
from jobfinder.util import make_session

ROOT = Path(__file__).resolve().parent


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("name", nargs="?")
    ap.add_argument("url", nargs="?")
    ap.add_argument("--file", help="text file with one 'Company name, careers URL' per line")
    ap.add_argument("--dry-run", action="store_true", help="show results without saving")
    args = ap.parse_args()

    pairs = []
    if args.file:
        for line in Path(args.file).read_text(encoding="utf-8").splitlines():
            if "," in line and not line.strip().startswith("#"):
                name, url = line.rsplit(",", 1)
                pairs.append((name.strip(), url.strip()))
    elif args.name and args.url:
        pairs.append((args.name, args.url))
    else:
        ap.error("give a name and URL, or --file")

    path = ROOT / "companies.yaml"
    companies = yaml.safe_load(path.read_text(encoding="utf-8")) or []
    known = {(c["ats"], str(c["id"]).lower()) for c in companies}
    session = make_session()
    added = []
    for name, url in pairs:
        try:
            found = detect(session, url)
        except Exception as e:
            print(f"  ?   {name}: could not open page ({str(e)[:80]})")
            continue
        if not found:
            print(f"  -   {name}: not detected (Workday/custom careers site?)")
            continue
        ats, board = found
        if (ats, board.lower()) in known:
            print(f"  =   {name}: already in list ({ats}: {board})")
            continue
        print(f"  +   {name}: {ats} / {board}")
        added.append({"name": name, "ats": ats, "id": board})
        known.add((ats, board.lower()))

    if added and not args.dry_run:
        with path.open("a", encoding="utf-8") as f:
            f.write("\n# added by detect_ats.py\n")
            for c in added:
                f.write(f"- {{name: \"{c['name']}\", ats: {c['ats']}, id: \"{c['id']}\"}}\n")
        print(f"\nAdded {len(added)} companies to companies.yaml. Test them: python run.py --check")


if __name__ == "__main__":
    main()
