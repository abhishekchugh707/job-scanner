"""Second opinion for jobs whose title has none of your role words: does the ad describe your
kind of work, even in other words ("model training pipelines" = MLOps, "Containerisierung" =
Docker)?

Model: intfloat/multilingual-e5-small (German + English), as an ONNX file (Xenova's export on
Hugging Face), run with onnxruntime on the CPU - about 120 MB, ~20 ms per job, no PyTorch.
It turns your profile and each ad into vectors; their cosine similarity says how close the ad is
to your work. The keyword skill points stay the main judge - the model only adds its opinion.

Everything here is optional: without the packages or the download the scanner works as before.
On first use it installs onnxruntime/tokenizers/huggingface_hub (pip, once), downloads the model
and calibrates its cut-off on your own answers (data/calibration.json) - see calibrate().
"""
from __future__ import annotations

import hashlib
import json
import re
import subprocess
import sys
import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable, Optional

REPO = "Xenova/multilingual-e5-small"
FILES = ("onnx/model_quantized.onnx", "tokenizer.json")
PACKAGES = ("onnxruntime", "tokenizers", "huggingface_hub")
STATE_FILE = "semantic_state.json"
CAL_VERSION = 3
DATA = Path(__file__).parent / "data" / "calibration.json"
ACTIVE: "Optional[Semantic]" = None        # set by the app once the model is loaded

_FOCUS = re.compile(r"(your (tasks|role|profile|responsibilities|mission|impact)|what you('ll| will)? (do|bring|build|need)|"
                    r"requirements|qualifications|responsibilities|about the role|the role|you bring|you have|"
                    r"aufgaben|dein profil|ihr profil|anforderungen|das bringst du mit|was du mitbringst|"
                    r"deine aufgaben|ihre aufgaben|wir erwarten|qualifikation)", re.I)


def focus_text(title: str, description: str, limit: int = 1800) -> str:
    """The part of an ad that says what the job is: from the first tasks/requirements heading on
    (the company introduction above it says little about the work)."""
    lines = [l.strip() for l in (description or "").splitlines() if l.strip()]
    start = next((i for i, l in enumerate(lines) if len(l) < 80 and _FOCUS.search(l)), 0)
    body = " \n".join(lines[start:])
    return f"{title}\n{body}"[:limit]


def profile_text(cfg: dict) -> str:
    p = cfg.get("profile") or {}
    f = cfg.get("filters") or {}
    skills = sorted((f.get("skills") or {}).items(), key=lambda kv: -kv[1])
    top = [k for k, w in skills if w >= 2][:30]
    roles = [r for r in (f.get("role_keywords") or [])][:25]
    return (f"{p.get('headline', '')}. Software engineer for machine learning, MLOps, backend and data. "
            f"Skills: {', '.join(top)}. Roles: {', '.join(roles)}.")


class Semantic:
    def __init__(self, root: Path, log: Callable[[str], None] = print):
        self.root, self.log = root, log
        self.session = self.tok = None
        self.ready = False
        self.status = "not started"
        self.profile_vec = None
        self.cal: dict = {}
        self._lock = threading.Lock()
        self._cache: dict = {}
        self.loading = False
        self.load_started: Optional[float] = None
        self.attempts = 0                    # failed loads in a row (retried with growing gaps)
        self.failed_at: Optional[float] = None
        self.stats = {"judged": 0, "lifted": 0, "errors": 0, "first_error": ""}

    def take_stats(self) -> dict:
        """What the model did since the last call (one scan)."""
        st, self.stats = self.stats, {"judged": 0, "lifted": 0, "errors": 0, "first_error": ""}
        return st

    def retry_due(self) -> bool:
        """Load failed earlier: try again after 20 min, 1 h, 3 h, then every 6 h. A failed
        package install isn't retried (it needs you to run pip once)."""
        import time
        if self.ready or self.loading or not self.attempts or self._state().get("install_failed"):
            return False
        wait = [20, 60, 180, 360][min(self.attempts, 4) - 1] * 60
        return time.time() - (self.failed_at or 0) >= wait

    # ---------------------------------------------------------------- setup
    def _state(self) -> dict:
        try:
            return json.loads((self.root / STATE_FILE).read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return {}

    def _save_state(self, st: dict) -> None:
        from .util import write_atomic
        write_atomic(self.root / STATE_FILE, json.dumps(st, indent=1, ensure_ascii=False))

    def _imports(self) -> bool:
        try:
            import numpy, onnxruntime, tokenizers, huggingface_hub  # noqa: F401
            return True
        except ImportError:
            return False

    def _install(self) -> bool:
        st = self._state()
        if st.get("install_failed"):
            return False
        self.status = "installing onnxruntime, tokenizers, huggingface_hub (one time)"
        self.log("Skills model: installing onnxruntime, tokenizers, huggingface_hub (one time)...")
        exe = sys.executable
        if exe.lower().endswith("pythonw.exe") and Path(exe[:-5] + ".exe").exists():
            exe = exe[:-5] + ".exe"                        # python.exe runs pip; no window is shown
        try:
            for extra in ([], ["--user"]):               # without admin rights: install for this user
                r = subprocess.run([exe, "-m", "pip", "install", "--disable-pip-version-check", "-q", *extra, *PACKAGES],
                                   capture_output=True, text=True, timeout=900,
                                   creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
                if r.returncode == 0:
                    break
            ok = r.returncode == 0
        except Exception as e:
            ok, r = False, None
            self.log(f"Skills model: install failed: {e}")
        if not ok:
            st["install_failed"] = (r.stderr or r.stdout)[-400:] if r else "pip not available"
            self._save_state(st)
            return False
        import importlib
        importlib.invalidate_caches()
        return self._imports()

    def load(self, cfg: dict, db=None) -> None:
        """Run in a background thread at start (and again later if it failed). Never raises."""
        import time
        if self.loading:
            return
        self.loading, self.load_started = True, time.time()
        try:
            self._load(cfg, db)
        finally:
            self.loading = False
            if self.ready:
                self.attempts, self.failed_at = 0, None
            else:
                self.attempts += 1
                self.failed_at = time.time()

    def _load(self, cfg: dict, db=None) -> None:
        try:
            if not self._imports() and not self._install():
                self.status = ("off: couldn't install onnxruntime/tokenizers/huggingface_hub - run "
                               "'pip install onnxruntime tokenizers huggingface_hub' once")
                self.log("Skills model: " + self.status)
                return
            self.status = "downloading the model (about 120 MB, one time)"
            from huggingface_hub import hf_hub_download
            import onnxruntime as ort
            from tokenizers import Tokenizer
            cache = self.root / "models"
            paths = [hf_hub_download(REPO, f, cache_dir=str(cache)) for f in FILES]
            so = ort.SessionOptions()
            so.intra_op_num_threads = 2                     # leave the laptop usable
            self.session = ort.InferenceSession(paths[0], sess_options=so, providers=["CPUExecutionProvider"])
            self.tok = Tokenizer.from_file(paths[1])
            self.tok.enable_truncation(512)
            self.tok.enable_padding(pad_id=self.tok.token_to_id("<pad>") or 1, pad_token="<pad>")
            self.profile_vec = self.embed(["query: " + profile_text(cfg)])[0]
            self.cal = self.calibrate(cfg, db)
            self.ready = True
            c = self.cal
            self.status = (f"on - cut-off {c.get('t_inbox')}: agrees with {c.get('agree')}/{c.get('n')} of your answers"
                           if c.get("t_inbox") is not None else "on - no calibration data, keywords decide alone")
            self.log("Skills model: " + self.status)
        except Exception as e:
            self.ready = False
            self.status = f"off: {str(e)[:160]} (keywords decide alone)"
            self.log("Skills model: " + self.status)

    # ---------------------------------------------------------------- embeddings
    def embed(self, texts: list[str]):
        import numpy as np
        out = []
        for i in range(0, len(texts), 16):
            enc = self.tok.encode_batch(texts[i:i + 16])
            ids = np.array([e.ids for e in enc], dtype=np.int64)
            mask = np.array([e.attention_mask for e in enc], dtype=np.int64)
            feeds = {"input_ids": ids, "attention_mask": mask}
            if any(x.name == "token_type_ids" for x in self.session.get_inputs()):
                feeds["token_type_ids"] = np.zeros_like(ids)
            with self._lock:
                hidden = self.session.run(None, feeds)[0]          # last_hidden_state
            m = mask[..., None].astype(np.float32)
            vec = (hidden * m).sum(1) / np.clip(m.sum(1), 1e-9, None)
            out.append(vec / np.linalg.norm(vec, axis=1, keepdims=True))
        return np.vstack(out)

    def similarity(self, title: str, description: str) -> Optional[float]:
        if self.profile_vec is None:
            return None
        k = hashlib.sha1((title + "\x00" + description).encode("utf-8", "ignore")).hexdigest()
        if k not in self._cache:
            v = self.embed(["passage: " + focus_text(title, description)])[0]
            self._cache[k] = round(float(v @ self.profile_vec), 4)
            if len(self._cache) > 20000:
                self._cache.clear()
        return self._cache[k]

    # ---------------------------------------------------------------- calibration
    def calibrate(self, cfg: dict, db=None) -> dict:
        """Pick the cut-off on your own answers (data/calibration.json: 30 borderline jobs you
        marked yes/no, plus clearly unrelated ads). Cached until the profile or data changes."""
        from . import filters
        from .models import Job
        if not DATA.exists():
            return {}
        data = json.loads(DATA.read_text(encoding="utf-8"))
        sig = hashlib.sha1((profile_text(cfg) + json.dumps(cfg.get("filters", {}).get("skills", {}), sort_keys=True)
                            + str(CAL_VERSION) + str(DATA.stat().st_size)).encode()).hexdigest()[:12]
        st = self._state()
        if st.get("cal", {}).get("sig") == sig:
            return st["cal"]
        self.status = "calibrating on your answers"
        fc = cfg["filters"]

        def points(title, desc):
            j = Job(source="cal", company="", title=title, url="cal", description=desc)
            filters.score(j, fc)
            return filters.skill_points(j, fc), len(filters.tech_skills(j.matched, fc))

        hard = []
        for h in data.get("hard", []):
            pts, n_tech = points(h["title"], h["description"])
            hard.append({**h, "sim": self.similarity(h["title"], h["description"]), "pts": pts, "tech": n_tech})
        negs = [self.similarity(n["title"], n["description"]) for n in data.get("unrelated", [])]
        mine = []
        if db is not None:
            rows = db.con.execute("SELECT data FROM jobs WHERE data != '' AND grp IN ('main','low','maybe')"
                                  " ORDER BY first_seen DESC LIMIT 400").fetchall()
            for (d,) in rows:
                d = json.loads(d)
                if len(d.get("description") or "") > 300:
                    mine.append(self.similarity(d["title"], d["description"]))
        cands = sorted({round(h["sim"], 3) for h in hard} | {round(x, 3) for x in negs})
        best = None
        for t in cands:
            agree = sum((decide(h["pts"], h["sim"], t, h["tech"]) == "inbox") == h["yes"] for h in hard)
            leak = sum(x >= t for x in negs) / max(1, len(negs))
            key = (agree, -leak, t)
            if leak <= 0.03 and (best is None or key > best[0]):
                best = (key, t)
        t = best[1] if best else None
        res = {"sig": sig, "at": datetime.now(timezone.utc).isoformat(timespec="seconds"), "t_inbox": t,
               "n": len(hard), "agree": best[0][0] if best else None,
               "unrelated_over": sum(x >= t for x in negs) if t is not None else None, "unrelated": len(negs),
               "yours_over": sum(x >= t for x in mine) if t is not None else None, "yours": len(mine),
               "hard": [{"n": h.get("n"), "title": h["title"][:70], "company": h.get("company", ""), "yes": h["yes"],
                         "sim": h["sim"], "pts": h["pts"],
                         "model_says": decide(h["pts"], h["sim"], t, h["tech"])} for h in hard]}
        st["cal"] = res
        self._save_state(st)
        return res

    def judge(self, pts: int, title: str, description: str, n_tech: int = 2) -> tuple[Optional[str], Optional[float]]:
        """'inbox', 'low' or None for a job without a role word in its title."""
        sim = None
        if self.ready and self.cal.get("t_inbox") is not None and len(description or "") > 200:
            try:
                sim = self.similarity(title, description)
                self.stats["judged"] += 1
            except Exception as e:
                sim = None
                self.stats["errors"] += 1
                if not self.stats["first_error"]:
                    self.stats["first_error"] = f"{type(e).__name__}: {str(e)[:150]}"
                    self.log(f"Skills model error on '{title[:60]}': {self.stats['first_error']}")
        verdict = decide(pts, sim, self.cal.get("t_inbox") if self.ready else None, n_tech)
        if verdict == "inbox" and decide(pts, None, None, n_tech) != "inbox":
            self.stats["lifted"] += 1                # in your Inbox thanks to the model
        return verdict, sim


def decide(pts: int, sim: Optional[float], t: Optional[float], n_tech: int = 2) -> Optional[str]:
    """For a title without your role words. pts: skill points (no freshness bonus); n_tech: how
    many of your technologies the ad names (domain words like energy don't count); sim: the
    model's similarity to your profile, t its cut-off.
    Inbox: clearly your skills, or the model sees your kind of work and the ad names at least one
    of your technologies. Low match: at least one of your technologies - better seen and skipped
    than missed. Otherwise dropped."""
    if n_tech >= 2 and pts >= 6:
        return "inbox"
    if sim is not None and t is not None and sim >= t and n_tech >= 1 and pts >= 0:
        return "inbox"
    if n_tech >= 1 and pts >= 1:
        return "low"
    return None


def keyword_only(pts: int, title: str, description: str, n_tech: int = 2):
    return decide(pts, None, None, n_tech), None
