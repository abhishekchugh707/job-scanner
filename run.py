"""Find new job openings.

    python run.py                 # normal run: only jobs you haven't seen yet
    python run.py --days 3        # only jobs posted in the last 3 days
    python run.py --force-linkedin   # read LinkedIn/Indeed even if read in the last hours
    python run.py --check         # test every company in companies.yaml, report broken ones
    python run.py --companies-only   # skip the job boards (fastest)
    python run.py --open          # open the results page in your browser when done
"""
from __future__ import annotations

import argparse
import webbrowser
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime
from pathlib import Path

from jobfinder import report, scan
from jobfinder.sources import FETCHERS
from jobfinder.util import make_session

ROOT = Path(__file__).resolve().parent


def load_yaml(name: str):
    return scan.load_yaml(name, ROOT)


def check_companies(companies, workers):
    session = make_session()
    print(f"Checking {len(companies)} company feeds...\n")
    with ThreadPoolExecutor(max_workers=workers) as pool:
        futs = {pool.submit(FETCHERS[c["ats"].lower()], session, c["name"], str(c["id"])): c
                for c in companies if c.get("ats", "").lower() in FETCHERS}
        bad = 0
        for fut in as_completed(futs):
            c = futs[fut]
            try:
                n = len(fut.result())
                print(f"  ok    {c['name']:<32} {c['ats']:<16} {n} open jobs")
            except Exception as e:
                bad += 1
                print(f"  FAIL  {c['name']:<32} {c['ats']:<16} {str(e)[:90]}")
    print(f"\n{bad} broken. Fix their id with: python detect_ats.py <careers page url>")


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--days", type=float, help="max posting age in days (default from config.yaml)")
    ap.add_argument("--force-linkedin", action="store_true", help="read LinkedIn/Indeed even if read recently")
    ap.add_argument("--check", action="store_true", help="test every company feed and exit")
    ap.add_argument("--companies-only", action="store_true", help="skip job boards")
    ap.add_argument("--open", action="store_true", help="open the HTML page when done")
    args = ap.parse_args()

    cfg = load_yaml("config.yaml")
    companies = load_yaml("companies.yaml") or []
    workers = int(cfg.get("parallel_requests", 16))

    if args.check:
        check_companies(companies, workers)
        return

    summary, res = scan.run_scan(ROOT, days=args.days, use_boards=not args.companies_only,
                                 force_linkedin=args.force_linkedin)
    out = ROOT / "output"
    out.mkdir(exist_ok=True)
    stamp = datetime.now().strftime("%Y-%m-%d_%H%M")
    report.write_csv(res.matched + res.maybe + res.unchecked, out / f"jobs_{stamp}.csv")

    a = summary["added"]
    print(f"\n{report.stats_line(summary['stats'])}  ({summary['seconds']}s)")
    print(f"New for you: {a['main']} matches, {a['maybe']} maybe, {a['unchecked']} German not checked")
    for w in summary["warnings"]:
        print(f"  ! {w}")
    print(f"\nEverything filtered out (with reasons): {out / 'filtered_out_latest.csv'}")
    print("See and mark the jobs in the dashboard: find_jobs.bat  (or python app.py)")
    if args.open:
        webbrowser.open("http://localhost:8765")


if __name__ == "__main__":
    main()
