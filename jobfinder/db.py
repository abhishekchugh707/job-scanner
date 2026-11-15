"""The job database (jobs.db, SQLite): every job ever shown, and what you did with it.

status:  new      found by a scan, you haven't opened the dashboard since
         seen     shown to you, no decision yet
         saved    starred for later
         applied  you applied
         skipped  not for you
A job that reappears from another site (same company + title) inherits the
status, so a job you applied to via its careers page doesn't pop up again
when LinkedIn lists it a day later.
"""
from __future__ import annotations

import json
import sqlite3
import threading
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Optional

from .models import Job, source_rank
from .tracking import norm_company, norm_title

STATUSES = ("new", "seen", "saved", "applied", "skipped")
_LOCK = threading.Lock()


def fingerprint(company: str, title: str) -> str:
    return norm_company(company or "")[:20] + "|" + norm_title(title)


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


class JobDB:
    def __init__(self, path: Path):
        self.path = path
        self.con = sqlite3.connect(path, check_same_thread=False, timeout=30)
        self.con.row_factory = sqlite3.Row
        with _LOCK, self.con:
            self.con.execute("PRAGMA journal_mode=WAL")
            self.con.executescript("""
            CREATE TABLE IF NOT EXISTS jobs (
                key TEXT PRIMARY KEY, fingerprint TEXT, grp TEXT, status TEXT,
                first_seen TEXT, last_seen TEXT, status_at TEXT, score INTEGER, data TEXT);
            CREATE INDEX IF NOT EXISTS jobs_fp ON jobs(fingerprint);
            CREATE INDEX IF NOT EXISTS jobs_first ON jobs(first_seen);
            CREATE TABLE IF NOT EXISTS meta (k TEXT PRIMARY KEY, v TEXT);
            CREATE TABLE IF NOT EXISTS rejected (
                key TEXT PRIMARY KEY, fingerprint TEXT, reason TEXT, first_seen TEXT, last_seen TEXT, data TEXT);
            CREATE INDEX IF NOT EXISTS rejected_fp ON rejected(fingerprint);
            """)

    # -------------------------------------------------------------- lookups
    def is_new(self, job: Job) -> bool:
        """True when neither this link nor the same company+title is known."""
        row = self.con.execute("SELECT 1 FROM jobs WHERE key=? OR fingerprint=? LIMIT 1",
                               (job.key, fingerprint(job.company, job.title))).fetchone()
        return row is None

    def needs_text(self, job: Job) -> bool:
        """Worth reading the full description: a job you haven't seen, one known only from
        seen_jobs.json, or one whose description couldn't be read yet (3 tries)."""
        row = self.con.execute("SELECT data, grp FROM jobs WHERE key=? OR fingerprint=? ORDER BY key=? DESC LIMIT 1",
                               (job.key, fingerprint(job.company, job.title), job.key)).fetchone()
        if row is None or not row["data"]:
            return True
        return row["grp"] == "unchecked" and json.loads(row["data"]).get("tries", 1) < 3

    # -------------------------------------------------------------- writes
    def add(self, jobs: list[Job], group: str, extra: Optional[dict] = None, initial: str = "new") -> int:
        """Store jobs from a scan. Unknown jobs become 'new'; returns how many were new.

        A job known under another link (same company + title) keeps its status: applied
        stays applied, and jobs imported from seen_jobs.json get their details filled in."""
        added, now = 0, _now()
        with _LOCK, self.con:
            for j in jobs:
                fp = fingerprint(j.company, j.title)
                data = json.dumps(_job_dict(j, (extra or {}).get(j.key, {})), ensure_ascii=False)
                row = self.con.execute("SELECT data, grp FROM jobs WHERE key=?", (j.key,)).fetchone()
                if row:
                    self.con.execute("UPDATE jobs SET last_seen=? WHERE key=?", (now, j.key))
                    if not row["data"] or row["grp"] == "unchecked":
                        # details now known, or the description could finally be read
                        if group == "unchecked" and row["data"]:
                            old = json.loads(row["data"])
                            d = json.loads(data)
                            d["tries"] = old.get("tries", 1) + 1
                            data = json.dumps(d, ensure_ascii=False)
                        self.con.execute("UPDATE jobs SET data=?, grp=?, score=? WHERE key=?",
                                         (data, group, j.score, j.key))
                    continue
                twins = self.con.execute("SELECT key, status, grp, data FROM jobs WHERE fingerprint=? ORDER BY first_seen",
                                         (fp,)).fetchall()
                if twins and any(t["data"] for t in twins):
                    if group != "unchecked":
                        # listed under another link without a readable description (e.g. a dead
                        # Personio page): now the text could be read - update it, keep its status
                        self.con.execute("UPDATE jobs SET data=?, grp=?, score=?, last_seen=?"
                                         " WHERE fingerprint=? AND grp='unchecked'", (data, group, j.score, now, fp))
                        # first seen on LinkedIn or a job board, now on the company's own careers
                        # page: the card gets the company's link (status and place in the list stay)
                        for t in twins:
                            if not t["data"] or t["grp"] != group:
                                continue
                            old = json.loads(t["data"])
                            if source_rank(j.source) < source_rank(old.get("source", "")) and old.get("url") != j.url:
                                d = json.loads(data)
                                d.update({k: old[k] for k in ("tries", "missing", "closed") if k in old})
                                d["also_on"] = old.get("url", "")
                                self.con.execute("UPDATE jobs SET data=?, score=?, last_seen=? WHERE key=?",
                                                 (json.dumps(d, ensure_ascii=False), j.score, now, t["key"]))
                            break
                    continue                       # already listed under another link
                status = twins[0]["status"] if twins else initial
                self.con.execute(
                    "INSERT INTO jobs(key, fingerprint, grp, status, first_seen, last_seen, status_at, score, data)"
                    " VALUES (?,?,?,?,?,?,?,?,?)",
                    (j.key, fp, group, status, now, now, now, j.score, data))
                added += not twins
        return added

    def set_status(self, key: str, status: str) -> bool:
        if status not in STATUSES:
            raise ValueError(status)
        with _LOCK, self.con:
            row = self.con.execute("SELECT fingerprint FROM jobs WHERE key=?", (key,)).fetchone()
            if not row:
                return False
            # the same job found on other sites follows along
            self.con.execute("UPDATE jobs SET status=?, status_at=? WHERE fingerprint=?",
                             (status, _now(), row["fingerprint"]))
        return True

    def add_manual(self, company: str, title: str, url: str = "", status: str = "applied") -> str:
        """A job you found yourself (e.g. on LinkedIn): the scanner will never show it as new."""
        if status not in STATUSES:
            raise ValueError(status)
        j = Job(source="manual", company=company.strip(), title=title.strip(),
                url=url.strip() or f"manual:{company}|{title}", location="")
        fp = fingerprint(j.company, j.title)
        now = _now()
        with _LOCK, self.con:
            self.con.execute("UPDATE jobs SET status=?, status_at=? WHERE fingerprint=?", (status, now, fp))
            self.con.execute(
                "INSERT OR IGNORE INTO jobs(key, fingerprint, grp, status, first_seen, last_seen, status_at, score,"
                " data) VALUES (?,?,?,?,?,?,?,?,?)",
                (j.key, fp, "manual", status, now, now, now, 0, json.dumps(_job_dict(j, {}), ensure_ascii=False)))
        return j.key

    def drop_open_jobs(self, keep) -> int:
        """Delete jobs you haven't acted on (new/seen) for which keep(data) is False - used when
        a filter is fixed, so jobs it wrongly let through disappear. Applied/saved/skipped stay."""
        with _LOCK, self.con:
            rows = self.con.execute("SELECT key, data FROM jobs WHERE data != '' AND status IN ('new','seen')").fetchall()
            bad = [r["key"] for r in rows if not keep(json.loads(r["data"]))]
            self.con.executemany("DELETE FROM jobs WHERE key=?", [(k,) for k in bad])
        return len(bad)

    def delete_groups(self, groups) -> int:
        """Delete every stored job of these groups (a tab that was switched off)."""
        marks = ",".join("?" * len(groups))
        with _LOCK, self.con:
            n = self.con.execute(f"DELETE FROM jobs WHERE grp IN ({marks})", tuple(groups)).rowcount
        return n

    def update_listed(self, listed: dict) -> int:
        """listed: feed label ("N26 (greenhouse)") -> fingerprints of every job that feed lists right
        now (only feeds that list all their jobs). A job of yours missing from its feed in 2 scans in
        a row is marked closed; if it shows up again, the mark goes. Returns how many got closed."""
        closed = 0
        with _LOCK, self.con:
            rows = self.con.execute("SELECT key, fingerprint, data FROM jobs WHERE data != ''"
                                    " AND status IN ('new','seen','saved')").fetchall()
            for r in rows:
                d = json.loads(r["data"])
                label = d.get("origin") or f"{d.get('company', '')} ({d.get('source', '')})"
                if label not in listed:
                    continue
                if r["fingerprint"] in listed[label]:
                    if not d.get("missing"):
                        continue
                    d["missing"], d["closed"] = 0, False
                else:
                    d["missing"] = d.get("missing", 0) + 1
                    if d["missing"] == 2:
                        d["closed"] = True
                        closed += 1
                self.con.execute("UPDATE jobs SET data=? WHERE key=?", (json.dumps(d, ensure_ascii=False), r["key"]))
        return closed

    # -------------------------------------------------------------- rejected jobs (kept 14 days)
    def add_rejected(self, rejected: list, keep_days: int = 14) -> None:
        """Every job a scan filtered out, with its text and the reason - so a rule change can be
        tested on the past, "why didn't I see this?" can be answered, and an ad's text that was
        already read isn't fetched again."""
        now = _now()
        with _LOCK, self.con:
            for j, reason in rejected:
                d = json.dumps({**_job_dict(j, {}), "description": (j.description or "")[:6000]}, ensure_ascii=False)
                self.con.execute(
                    "INSERT INTO rejected(key, fingerprint, reason, first_seen, last_seen, data) VALUES (?,?,?,?,?,?)"
                    " ON CONFLICT(key) DO UPDATE SET reason=excluded.reason, last_seen=excluded.last_seen,"
                    " data=CASE WHEN length(excluded.data) >= length(rejected.data) THEN excluded.data ELSE rejected.data END",
                    (j.key, fingerprint(j.company, j.title), reason, now, now, d))
            cutoff = (datetime.now(timezone.utc) - timedelta(days=keep_days)).isoformat()
            self.con.execute("DELETE FROM rejected WHERE last_seen < ?", (cutoff,))

    def known_text(self, job: Job) -> str:
        """A description already read for this job in an earlier scan (rejected or stored)."""
        for table in ("rejected", "jobs"):
            row = self.con.execute(f"SELECT data FROM {table} WHERE key=? AND data != ''", (job.key,)).fetchone()
            if row:
                d = json.loads(row["data"])
                if len(d.get("description") or "") > 200:
                    return d["description"]
        return ""

    def find(self, url: str = "", key: str = "", company: str = "", title: str = "") -> dict:
        """Where a job ended up: {"stored": {...}} or {"rejected": {...}} or {}."""
        fp = fingerprint(company, title) if title else None
        for table in ("jobs", "rejected"):
            cols = "key, fingerprint, data, first_seen" + (", reason" if table == "rejected" else ", grp, status, status_at")
            row = None
            if key:
                row = self.con.execute(f"SELECT {cols} FROM {table} WHERE key=?", (key,)).fetchone()
            if row is None and url:
                # the job's own link, or the link it applies through (a board ad pointing at the company page)
                row = self.con.execute(f"SELECT {cols} FROM {table} WHERE data LIKE ? OR data LIKE ? LIMIT 1",
                                       (f'%"url": "{url}%', f'%"apply_url": "{url}%')).fetchone()
            if row is None and fp:
                row = self.con.execute(f"SELECT {cols} FROM {table} WHERE fingerprint=? LIMIT 1", (fp,)).fetchone()
            if row:
                d = dict(row)
                d["data"] = json.loads(d["data"]) if d.get("data") else {}
                return {("stored" if table == "jobs" else "rejected"): d}
        return {}

    def mark_new_as_seen(self) -> None:
        with _LOCK, self.con:
            self.con.execute("UPDATE jobs SET status='seen' WHERE status='new'")

    # -------------------------------------------------------------- reads
    def jobs(self, days: int = 21) -> list[dict]:
        since = (datetime.now(timezone.utc) - timedelta(days=days)).isoformat()
        rows = self.con.execute(
            "SELECT * FROM jobs WHERE data != '' AND (first_seen >= ? OR status IN ('saved','applied'))"
            " ORDER BY first_seen DESC", (since,)).fetchall()
        out, seen_fp = [], set()
        for r in rows:
            if r["fingerprint"] in seen_fp:          # show each job once
                continue
            seen_fp.add(r["fingerprint"])
            d = json.loads(r["data"])
            d.update(key=r["key"], group=r["grp"], status=r["status"], first_seen=r["first_seen"],
                     status_at=r["status_at"])
            out.append(d)
        return out

    def counts(self) -> dict:
        rows = self.con.execute("SELECT status, COUNT(*) n FROM jobs WHERE data != '' GROUP BY status").fetchall()
        return {r["status"]: r["n"] for r in rows}

    # -------------------------------------------------------------- meta
    def get(self, k: str, default=None):
        row = self.con.execute("SELECT v FROM meta WHERE k=?", (k,)).fetchone()
        return json.loads(row["v"]) if row else default

    def put(self, k: str, v) -> None:
        with _LOCK, self.con:
            self.con.execute("INSERT INTO meta(k, v) VALUES(?, ?) ON CONFLICT(k) DO UPDATE SET v=excluded.v",
                             (k, json.dumps(v, ensure_ascii=False)))

    # -------------------------------------------------------------- upkeep
    def import_seen_file(self, path: Path) -> int:
        """Jobs from the old seen_jobs.json count as seen (stored without details)."""
        if not path.exists() or self.get("imported_seen_json"):
            return 0
        try:
            old = json.loads(path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            old = {}
        n = 0
        with _LOCK, self.con:
            for key, v in old.items():
                fp = fingerprint(v.get("company", ""), v.get("title", ""))
                cur = self.con.execute(
                    "INSERT OR IGNORE INTO jobs(key, fingerprint, grp, status, first_seen, last_seen, status_at,"
                    " score, data) VALUES (?,?,?,?,?,?,?,?,?)",
                    (key, fp, "main", "seen", v.get("first_seen", _now()), _now(), _now(), 0, ""))
                n += cur.rowcount
        self.put("imported_seen_json", True)
        return n

    def prune(self, keep_days: int = 90) -> None:
        cutoff = (datetime.now(timezone.utc) - timedelta(days=keep_days)).isoformat()
        with _LOCK, self.con:
            self.con.execute("DELETE FROM jobs WHERE last_seen < ? AND status IN ('seen','skipped','new')",
                             (cutoff,))


def _job_dict(j: Job, extra: dict) -> dict:
    return {
        "title": j.title, "company": j.company, "url": j.url, "location": j.location,
        "source": j.source, "origin": j.origin, "apply_url": j.apply_url, "company_url": j.company_url, "posted": j.posted.isoformat() if j.posted else None,
        "score": j.score, "matched": j.matched, "gaps": j.gaps, "flags": j.flags,
        "remote": j.remote, "german_ad": j.german_posting,
        "description": (j.description or "")[:8000], **extra,
    }
