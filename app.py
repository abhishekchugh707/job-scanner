"""Job Scanner: the dashboard plus the hourly background scan.

    python app.py            start it (normally done by Windows at logon, see setup_autostart.bat)
    python app.py --open     start it if needed and open the dashboard
Dashboard: http://localhost:8765
"""
from __future__ import annotations

import argparse
import json
import os
import time
import socket
import sys
import threading
import traceback
import webbrowser
from datetime import datetime, timedelta, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

ROOT = Path(__file__).resolve().parent
PORT = 8765
LOG = ROOT / "app.log"

# pythonw (no console window) has no stdout: send prints to app.log
if sys.stdout is None or sys.stderr is None or "--log" in sys.argv:
    _f = open(LOG, "a", encoding="utf-8", buffering=1)
    sys.stdout = sys.stderr = _f

from jobfinder import discover, network, opendata, scan, semantic, why   # noqa: E402
from jobfinder.util import make_session      # noqa: E402
from jobfinder.db import STATUSES, JobDB      # noqa: E402

PAGE = ROOT / "jobfinder" / "web" / "dashboard.html"


class Scanner:
    def __init__(self):
        self.lock = threading.Lock()
        self.running = False
        self.started = None
        self.lines: list[str] = []
        self.next_run = None
        self.wake = threading.Event()
        self.force_linkedin = False
        self.force_full = False
        self.looping = False

    def log(self, msg: str) -> None:
        line = f"{datetime.now():%H:%M:%S}  {msg}"
        print(line, flush=True)
        self.lines = (self.lines + [line])[-40:]

    def interval(self) -> float:
        try:
            return float(scan.load_yaml("config.yaml", ROOT).get("scan_every_minutes", 60))
        except Exception:
            return 60.0

    def run_once(self) -> None:
        if not self.lock.acquire(blocking=False):
            return
        self.running, self.started, self.lines = True, datetime.now(timezone.utc), []
        try:
            self.log("Scan started")
            summary, _ = scan.run_scan(ROOT, force_linkedin=self.force_linkedin, log=self.log,
                                       full=True if self.force_full else None)
            a = summary["added"]
            self.log(f"Done in {summary['seconds']}s: {a['main']} new matches, {a['maybe']} maybe, "
                     f"{a['unchecked']} German not checked, {a.get('research', 0)} research"
                     + (f", {a['india']} India" if a.get("india") else ""))
        except Exception as e:
            self.log(f"Scan failed: {e}")
            traceback.print_exc()
            db = JobDB(ROOT / "jobs.db")
            db.put("last_error", {"at": datetime.now(timezone.utc).isoformat(timespec="seconds"), "error": str(e)})
        finally:
            _background_jobs()
            self.force_linkedin = False
            self.force_full = False
            self.running = False
            self.lock.release()

    def loop(self) -> None:
        self.looping = True
        db = JobDB(ROOT / "jobs.db")
        last = (db.get("last_scan") or {}).get("finished")
        db.con.close()
        due = datetime.now(timezone.utc)
        if last:
            due = datetime.fromisoformat(last) + timedelta(minutes=self.interval())
        while True:
            now = datetime.now(timezone.utc)
            if now >= due:
                try:
                    self.run_once()
                except Exception:                  # never let the hourly loop die
                    traceback.print_exc()
                due = datetime.now(timezone.utc) + timedelta(minutes=self.interval())
            self.next_run = due
            # sleep in short steps: a laptop waking from sleep scans right away
            self.wake.wait(timeout=30)
            if self.wake.is_set():
                self.wake.clear()
                due = datetime.now(timezone.utc)


SCANNER = Scanner()
PROBER = network.Prober(ROOT, make_session, log=lambda m: SCANNER.log(m))
OPENDATA = opendata.OpenData(ROOT, make_session, log=lambda m: SCANNER.log(m))


MODEL = semantic.Semantic(ROOT, log=lambda m: SCANNER.log(m))
semantic.ACTIVE = MODEL               # scans use it once it's ready, and report its health


def _load_model() -> None:
    """Skills model (second opinion for titles without your role words) - in the background;
    until it's ready, or if it can't load, the keyword skill points decide alone."""
    try:
        cfg = scan.load_yaml("config.yaml", ROOT)
        if not (cfg.get("skills_model") or {}).get("enabled", True):
            MODEL.status = "off in config.yaml"
            return
        db = JobDB(ROOT / "jobs.db")
        try:
            MODEL.load(cfg, db)
        finally:
            db.con.close()
    except Exception as e:
        traceback.print_exc()
        scan.record_background_error(ROOT, "skills model", e)


LOOKUP_LOCK = threading.Lock()


def _lookup_names() -> None:
    if OPENDATA.running or OPENDATA.due():
        return                    # wait for the fresh company-name index (it runs again after the next scan)
    if not LOOKUP_LOCK.acquire(blocking=False):
        return
    try:
        discover.lookup_queued(make_session(), ROOT, log=lambda m: SCANNER.log(m))
    except Exception as e:
        traceback.print_exc()
        scan.record_background_error(ROOT, "company careers-page lookup", e)
    finally:
        LOOKUP_LOCK.release()


def _background_jobs() -> None:
    """After a scan: careers-page lookups of job-board companies, the network check (if
    unfinished) and the weekly open-data company search."""
    threading.Thread(target=_lookup_names, daemon=True).start()
    if MODEL.retry_due():                         # the model failed to load earlier: try again
        threading.Thread(target=_load_model, daemon=True).start()
    if (ROOT / network.CSV_PATH).exists() and not PROBER.running:
        threading.Thread(target=PROBER.run, daemon=True).start()
    if not OPENDATA.running and OPENDATA.due():
        cfg = scan.load_yaml("config.yaml", ROOT)
        if (cfg.get("open_data") or {}).get("enabled", True):
            threading.Thread(target=OPENDATA.run, args=(cfg["filters"], discover.load_companies(ROOT)),
                             daemon=True).start()


def _network_status() -> dict:
    net = network.load(ROOT)
    if not net:
        return {}
    return {"companies": len(net), "people": sum(len(e["people"]) for e in net.values()),
            "running": PROBER.running, **PROBER.progress}


def _profile() -> dict:
    cfg = scan.load_yaml("config.yaml", ROOT)
    p = cfg.get("profile") or {}
    home = scan.home_of(cfg)
    skills = sorted((cfg.get("filters") or {}).get("skills", {}).items(), key=lambda kv: -kv[1])
    return {"name": p.get("name", ""), "headline": p.get("headline", ""),
            "home": home["name"], "near_km": home["near_km"],
            "top_skills": p.get("top_skills") or [k for k, _ in skills[:10]],
            "interval": SCANNER.interval(),
            "network_label": p.get("network_label", "Your network")}


def _auto_companies() -> list:
    try:
        from jobfinder.discover import AUTO_FILE
        import yaml
        p = ROOT / AUTO_FILE
        return (yaml.safe_load(p.read_text(encoding="utf-8")) or []) if p.exists() else []
    except Exception:
        return []


def _report(db: JobDB) -> dict:
    """Everything needed to check a scan by hand: the funnel, every source, every dropped job."""
    import csv
    rejected = []
    p = ROOT / "output" / "filtered_out_latest.csv"
    if p.exists():
        with p.open(encoding="utf-8-sig", newline="") as f:
            for r in csv.DictReader(f):
                rejected.append(r)
    net_rows = []
    np = ROOT / network.OUT_FILE
    if np.exists():
        import yaml
        net_rows = yaml.safe_load(np.read_text(encoding="utf-8")) or []
    daily_total = len(discover.load_companies(ROOT, discover.DAILY_FILES))
    return {"last_scan": db.get("last_scan") or {}, "daily_total": daily_total, "rejected": rejected[:6000], "rejected_total": len(rejected),
            "network": _network_status(), "network_companies": net_rows,
            "opendata": {**OPENDATA._state(), "running": OPENDATA.running, **OPENDATA.progress,
                         "companies": _yaml_list(opendata.OUT_FILE)},
            "db": [dict(r) for r in db.con.execute(
                "SELECT grp, status, COUNT(*) n FROM jobs WHERE data != '' GROUP BY grp, status").fetchall()],
            "model": {"status": MODEL.status, "ready": MODEL.ready,
                      **{k: v for k, v in (MODEL.cal or {}).items() if k != "sig"}},
            "why_cases": why.cases(ROOT)[-20:],
            "log": _log_tail(80)}


def _yaml_list(name: str) -> list:
    import yaml
    p = ROOT / name
    try:
        return (yaml.safe_load(p.read_text(encoding="utf-8")) or []) if p.exists() else []
    except Exception:
        return []


def _log_tail(n: int) -> list:
    try:
        with LOG.open(encoding="utf-8", errors="replace") as f:
            lines = [l.rstrip() for l in f.readlines()[-400:]]
        return [l for l in lines if l and "skipping job" not in l and "no job data returned" not in l][-n:]
    except OSError:
        return SCANNER.lines[-n:]


class Server(ThreadingHTTPServer):
    daemon_threads = True

    def handle_error(self, request, client_address):
        # the browser closed the tab or reloaded mid-answer: not an error worth logging
        if isinstance(sys.exc_info()[1], (ConnectionError, TimeoutError)):
            return
        super().handle_error(request, client_address)


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *a):          # keep app.log quiet
        pass

    def _send(self, code: int, body, ctype="application/json"):
        data = body if isinstance(body, bytes) else json.dumps(body, ensure_ascii=False).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", ctype if ctype.startswith("font/") else ctype + "; charset=utf-8")
        self.send_header("Cache-Control", "no-store")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def _body(self) -> dict:
        n = int(self.headers.get("Content-Length") or 0)
        try:
            return json.loads(self.rfile.read(n) or b"{}")
        except json.JSONDecodeError:
            return {}

    def _status(self, db: JobDB) -> dict:
        last = db.get("last_scan") or {}
        return {"running": SCANNER.running, "lines": SCANNER.lines[-8:],
                "next_run": SCANNER.next_run.isoformat() if SCANNER.next_run else None,
                "last_scan": last, "last_error": db.get("last_error"),
                "counts": (c := db.counts()), "new": c.get("new", 0),
                "auto_companies": _auto_companies(), "network": _network_status()}

    def do_GET(self):
        path = self.path.split("?")[0]
        if path in ("/", "/index.html"):
            return self._send(200, PAGE.read_bytes(), "text/html")
        if path == "/roboto-flex.woff2":
            return self._send(200, (PAGE.parent / "roboto-flex.woff2").read_bytes(), "font/woff2")
        db = JobDB(ROOT / "jobs.db")
        try:
            if path == "/api/jobs":
                jobs = db.jobs()
                net = network.load(ROOT)
                for j in jobs:                    # who he knows there, always from the current file
                    j["contacts"] = network.contacts(net, j.get("company", "")) if net else None
                return self._send(200, {"jobs": jobs, "status": self._status(db), "profile": _profile()})
            if path == "/api/report":
                return self._send(200, _report(db))
            if path == "/api/status":
                return self._send(200, self._status(db))
            if path == "/api/ping":
                return self._send(200, {"app": "job-radar"})
        finally:
            db.con.close()
        self._send(404, {"error": "not found"})

    def do_POST(self):
        path, body = self.path.split("?")[0], self._body()
        if self.headers.get("Origin") not in (None, f"http://localhost:{PORT}", f"http://127.0.0.1:{PORT}"):
            return self._send(403, {"error": "origin"})       # other websites can't change your list
        db = JobDB(ROOT / "jobs.db")
        try:
            if path == "/api/scan":
                SCANNER.force_linkedin = bool(body.get("linkedin"))
                SCANNER.force_full = bool(body.get("full"))
                if not SCANNER.running:
                    if SCANNER.looping:
                        SCANNER.wake.set()
                    else:
                        threading.Thread(target=SCANNER.run_once, daemon=True).start()
                return self._send(200, {"ok": True})
            if path == "/api/state":
                if body.get("status") not in STATUSES:
                    return self._send(400, {"error": "bad status"})
                keys = body.get("keys") or [body.get("key")]
                ok = all(db.set_status(k, body["status"]) for k in keys if k)
                return self._send(200, {"ok": ok})
            if path == "/api/why":
                url = str(body.get("url") or "").strip()
                if not url.startswith("http"):
                    return self._send(400, {"error": "paste a full link (https://...)"})
                cfg = scan.load_yaml("config.yaml", ROOT)
                out = why.explain(ROOT, db, make_session(), url, cfg,
                                  judge=MODEL.judge if MODEL.ready else None)
                return self._send(200, out)
            if path == "/api/seen-all":
                db.mark_new_as_seen()
                return self._send(200, {"ok": True})
            if path == "/api/manual":
                if not (body.get("company") and body.get("title")):
                    return self._send(400, {"error": "company and title needed"})
                key = db.add_manual(body["company"], body["title"], body.get("url", ""),
                                    body.get("status", "applied"))
                return self._send(200, {"ok": True, "key": key})
        finally:
            db.con.close()
        self._send(404, {"error": "not found"})


def _code_stamp() -> float:
    files = [ROOT / "app.py", *(ROOT / "jobfinder").rglob("*.py")]
    return max((f.stat().st_mtime for f in files if f.exists()), default=0)


def _reload_when_code_changes(server) -> None:
    """When the code is updated (new version copied in), restart with it - between scans,
    so no scan is cut off. Progress of the network check is saved every 20 s and resumes."""
    import subprocess
    stamp = _code_stamp()
    while True:
        time.sleep(20)
        try:
            if _code_stamp() <= stamp or SCANNER.running:
                continue
            time.sleep(10)                        # let the copy finish
            print(f"{datetime.now():%H:%M:%S}  Code updated - restarting", flush=True)
            server.shutdown()                     # stop serving cleanly, then free the port
            server.server_close()
            exe = sys.executable
            if exe.lower().endswith("python.exe") and (Path(exe).parent / "pythonw.exe").exists():
                exe = str(Path(exe).parent / "pythonw.exe")
            flags = getattr(subprocess, "CREATE_NO_WINDOW", 0) | getattr(subprocess, "DETACHED_PROCESS", 0)
            args = [a for a in sys.argv[1:] if a not in ("--open", "--restarted")]
            subprocess.Popen([exe, str(ROOT / "app.py"), *args, "--restarted"], cwd=str(ROOT), creationflags=flags, close_fds=True)
            os._exit(0)
        except Exception:
            traceback.print_exc()


def already_running() -> bool:
    try:
        with socket.create_connection(("127.0.0.1", PORT), timeout=1) as s:
            s.sendall(b"GET /api/ping HTTP/1.0\r\n\r\n")
            return b"job-radar" in s.recv(4096)
    except OSError:
        return False


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--open", action="store_true", help="open the dashboard in the browser")
    ap.add_argument("--no-scan", action="store_true", help="dashboard only, no background scans")
    ap.add_argument("--log", action="store_true", help="write output to app.log")
    ap.add_argument("--restarted", action="store_true", help=argparse.SUPPRESS)
    args = ap.parse_args()
    url = f"http://localhost:{PORT}"
    if not args.restarted and already_running():
        if args.open:
            webbrowser.open(url)
        return
    db = JobDB(ROOT / "jobs.db")
    db.import_seen_file(ROOT / "seen_jobs.json")
    db.con.close()
    for i in range(30):                           # after a self-restart the old one may still hold the port
        try:
            server = Server(("127.0.0.1", PORT), Handler)
            break
        except OSError:
            time.sleep(1)
    else:
        print("Port 8765 is busy - is Job Scanner already running?", flush=True)
        return
    threading.Thread(target=_reload_when_code_changes, args=(server,), daemon=True).start()
    if not args.no_scan:
        threading.Thread(target=_load_model, daemon=True).start()
        threading.Thread(target=SCANNER.loop, daemon=True).start()
        _background_jobs()
    print(f"{datetime.now():%Y-%m-%d %H:%M}  Job Scanner running at {url}", flush=True)
    if args.open:
        threading.Timer(1.0, webbrowser.open, (url,)).start()
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        return
    except (OSError, ValueError):
        pass
    while True:                                   # serving stopped for a self-restart: os._exit follows
        time.sleep(60)


if __name__ == "__main__":
    main()
