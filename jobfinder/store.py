"""Remembers jobs already shown so each run lists only new ones."""
from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

from .models import Job

KEEP_DAYS = 120   # forget entries older than this


class SeenStore:
    def __init__(self, path: Path):
        self.path = path
        self.data: dict[str, dict] = {}
        if path.exists():
            try:
                self.data = json.loads(path.read_text(encoding="utf-8"))
            except json.JSONDecodeError:
                self.data = {}

    def is_new(self, job: Job) -> bool:
        return job.key not in self.data

    def add(self, job: Job) -> None:
        self.data[job.key] = {
            "first_seen": datetime.now(timezone.utc).isoformat(timespec="seconds"),
            "company": job.company, "title": job.title, "url": job.url,
        }

    def save(self) -> None:
        cutoff = datetime.now(timezone.utc) - timedelta(days=KEEP_DAYS)
        self.data = {k: v for k, v in self.data.items()
                     if datetime.fromisoformat(v["first_seen"]) >= cutoff}
        self.path.write_text(json.dumps(self.data, indent=1, ensure_ascii=False), encoding="utf-8")
