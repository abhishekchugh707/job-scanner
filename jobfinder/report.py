"""Writes the results: a CSV and a self-contained HTML page."""
from __future__ import annotations

import csv
import html
import json
from datetime import datetime
from pathlib import Path

from .models import Job


def _age(job: Job) -> str:
    h = job.age_hours
    if h is None:
        return "?"
    if h < 1:
        return "<1h"
    if h < 48:
        return f"{int(h)}h"
    return f"{int(h // 24)}d"


def write_csv(jobs: list[Job], path: Path) -> None:
    with path.open("w", newline="", encoding="utf-8-sig") as f:   # utf-8-sig: opens cleanly in Excel
        w = csv.writer(f)
        w.writerow(["score", "posted", "age", "company", "title", "location", "source",
                    "matched_skills", "flags", "url"])
        for j in jobs:
            w.writerow([j.score, j.posted.isoformat(timespec="minutes") if j.posted else "",
                        _age(j), j.company, j.title, j.location, j.source,
                        ", ".join(j.matched), " | ".join(j.flags), j.url])


def write_html(jobs: list[Job], path: Path, stats: dict, maybe=None, unchecked=None, warnings=None,
               suggestions=None, failed=None) -> None:
    rows = ([_row(j, "main") for j in jobs] + [_row(j, "maybe") for j in (maybe or [])]
            + [_row(j, "unchecked") for j in (unchecked or [])])
    extra = {"warnings": warnings or [], "failed": failed or [],
             "suggestions": [{"name": s["name"], "count": s["count"], "titles": s.get("titles", [])}
                             for s in (suggestions or [])]}
    safe = lambda o: json.dumps(o, ensure_ascii=False).replace("</", "<\\/")
    page = TEMPLATE.replace("__DATA__", safe(rows)).replace("__EXTRA__", safe(extra))
    page = page.replace("__STAMP__", html.escape(datetime.now().strftime("%a %d %b %Y, %H:%M")))
    page = page.replace("__STATS__", html.escape(stats_line(stats)))
    path.write_text(page, encoding="utf-8")


def _row(j: Job, group: str) -> dict:
    return {
        "k": j.key, "score": j.score, "age": _age(j), "g": group,
        "ageh": round(j.age_hours, 1) if j.age_hours is not None else 99999,
        "company": j.company, "title": j.title, "url": j.url,
        "location": j.location, "source": j.source,
        "matched": j.matched, "flags": j.flags,
        "jd": f"{j.title}\n{j.company} - {j.location}\n{j.url}\n\n{j.description}".strip(),
    }


def write_rejected(rejected: list[tuple[Job, str]], path: Path) -> None:
    """Every recent job that was filtered out, with the reason - to check nothing good was lost."""
    with path.open("w", newline="", encoding="utf-8-sig") as f:
        w = csv.writer(f)
        w.writerow(["reason", "company", "title", "location", "source", "age", "url"])
        for j, reason in sorted(rejected, key=lambda x: (x[1], x[0].company.lower())):
            w.writerow([reason, j.company, j.title, j.location, j.source, _age(j), j.url])


def stats_line(s: dict) -> str:
    maybe = f" + {s['maybe']} maybe" if s.get("maybe") else ""
    unchecked = f" + {s['unchecked']} not language-checked" if s.get("unchecked") else ""
    return (f"{s.get('fetched', 0)} jobs read from {s.get('sources_ok', 0)} sources · "
            f"{s.get('new', 0)} new matches{maybe}{unchecked} · filtered out: {s.get('title', 0)} other roles, "
            f"{s.get('location', 0)} outside Germany, {s.get('german', 0)} need fluent German, "
            f"{s.get('too_old', 0)} too old")


TEMPLATE = r"""<!doctype html>
<html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>New Job Openings</title>
<style>
:root{--bg:#f7f6f3;--card:#fff;--ink:#1d1d1b;--mute:#6b6a65;--line:#e4e2dc;--acc:#1f6f5c;--warn:#a5520f;--chip:#eef3f1}
@media (prefers-color-scheme:dark){:root{--bg:#161615;--card:#1f1f1d;--ink:#ecebe6;--mute:#9b9a93;--line:#33322f;--acc:#5fc4a8;--warn:#e39a5b;--chip:#243430}}
*{box-sizing:border-box}body{margin:0;background:var(--bg);color:var(--ink);font:15px/1.45 system-ui,-apple-system,"Segoe UI",sans-serif}
header{padding:28px 16px 8px;max-width:1100px;margin:auto}
h1{margin:0 0 4px;font-size:24px;letter-spacing:-.01em}.sub{color:var(--mute);font-size:13px}
.bar{display:flex;gap:8px;flex-wrap:wrap;max-width:1100px;margin:14px auto 6px;padding:0 16px}
input[type=search],select{font:inherit;padding:8px 10px;border:1px solid var(--line);border-radius:8px;background:var(--card);color:var(--ink)}
input[type=search]{flex:1;min-width:200px}
label.tog{display:flex;align-items:center;gap:6px;color:var(--mute);font-size:13px}
main{max-width:1100px;margin:auto;padding:6px 16px 40px}
.job{background:var(--card);border:1px solid var(--line);border-radius:10px;padding:12px 14px;margin:8px 0;display:grid;grid-template-columns:52px 1fr auto;gap:12px;align-items:start}
.job.done{opacity:.45}
.sc{font-weight:700;font-size:20px;color:var(--acc);text-align:center;line-height:1}
.sc small{display:block;font-size:11px;font-weight:500;color:var(--mute);margin-top:4px}
.t a{color:var(--ink);font-weight:600;text-decoration:none}.t a:hover{text-decoration:underline}
.meta{color:var(--mute);font-size:13px;margin-top:2px}
.chips{margin-top:6px;display:flex;flex-wrap:wrap;gap:4px}
.chip{background:var(--chip);border-radius:20px;padding:1px 8px;font-size:12px}
.flag{color:var(--warn);font-size:12px;margin-top:4px}
.act{display:flex;flex-direction:column;gap:6px;align-items:flex-end}
button{font:inherit;font-size:12px;padding:5px 10px;border:1px solid var(--line);border-radius:7px;background:transparent;color:var(--ink);cursor:pointer;white-space:nowrap}
button:hover{border-color:var(--acc)}
.empty{color:var(--mute);text-align:center;padding:40px}
h2{font-size:17px;margin:28px 0 2px}.h2sub{color:var(--mute);font-size:13px;margin-bottom:6px}
.job.maybe .sc{color:var(--mute)}
.alert{max-width:1100px;margin:10px auto 0;padding:0 16px}
.alert div{border:1px solid var(--warn);border-radius:10px;padding:10px 14px;font-size:13px;background:var(--card)}
.alert b{color:var(--warn)}.alert ul{margin:6px 0 0;padding-left:18px}
details{background:var(--card);border:1px solid var(--line);border-radius:10px;padding:10px 14px;margin-top:24px;font-size:14px}
summary{cursor:pointer;font-weight:600}details li{margin:4px 0}details .mute{color:var(--mute);font-size:12px}
code{background:var(--chip);padding:1px 5px;border-radius:5px;font-size:12px}
@media (max-width:640px){.job{grid-template-columns:40px 1fr}.act{grid-column:1/-1;flex-direction:row;justify-content:flex-end}}
</style></head><body>
<header><h1>New job openings</h1>
<div class="sub">Run __STAMP__ · __STATS__</div>
<div class="sub" style="margin-top:4px">Score = your CV skills found in the job (in the title counts double) · +3 if under 48h old · −5 if it asks for 5+ years.
Roughly: <b style="color:var(--acc)">15+ strong</b> · 8–14 good · under 8 weak or not enough info (LinkedIn / Bundesagentur often send no description).</div></header>
<div class="alert" id="alert"></div>
<div class="bar">
<input type="search" id="q" placeholder="Filter by company, title, skill, city…">
<select id="sort"><option value="score">Best match first</option><option value="age">Newest first</option></select>
<label class="tog"><input type="checkbox" id="hide"> Hide applied/skipped</label>
</div>
<main><div id="list"></div><div id="maybe"></div><div id="sugg"></div></main>
<script>
const DATA=__DATA__;
const EXTRA=__EXTRA__;
const KEY="jobfinder-done";
let done={};try{done=JSON.parse(localStorage.getItem(KEY)||"{}")}catch(e){}
function save(){try{localStorage.setItem(KEY,JSON.stringify(done))}catch(e){}}
const esc=s=>String(s).replace(/[&<>"]/g,c=>({"&":"&amp;","<":"&lt;",">":"&gt;",'"':"&quot;"}[c]));
function render(){
 const q=document.getElementById("q").value.toLowerCase(),hide=document.getElementById("hide").checked,
 by=document.getElementById("sort").value;
 let rows=DATA.filter(r=>!(hide&&done[r.k])&&(!q||[r.company,r.title,r.location,r.matched.join(" ")].join(" ").toLowerCase().includes(q)));
 rows.sort(by==="age"?(a,b)=>a.ageh-b.ageh:(a,b)=>b.score-a.score||a.ageh-b.ageh);
 const main=rows.filter(r=>r.g==="main"),maybe=rows.filter(r=>r.g==="maybe"),unc=rows.filter(r=>r.g==="unchecked");
 document.getElementById("list").innerHTML=main.length?cards(main):'<div class="empty">No new matches this run.</div>';
 document.getElementById("maybe").innerHTML=(maybe.length?`<h2>Maybe (${maybe.length})</h2>
 <div class="h2sub">The title doesn't use your role words, but the description matches your skills strongly. Worth a look.</div>${cards(maybe)}`:"")
 +(unc.length?`<h2>German not checked (${unc.length})</h2>
 <div class="h2sub">The full job description couldn't be read for these, so the "no fluent German" check could not run. Open each one and check the language requirement yourself.</div>${cards(unc)}`:"");
}
function cards(rows){
 return rows.map(r=>`<div class="job ${r.g!=="main"?"maybe":""} ${done[r.k]?"done":""}">
 <div class="sc">${r.score}<small>${esc(r.age)}</small></div>
 <div><div class="t"><a href="${esc(r.url)}" target="_blank" rel="noopener">${esc(r.title)}</a></div>
 <div class="meta">${esc(r.company)} · ${esc(r.location||"location not given")} · ${esc(r.source)}</div>
 ${r.matched.length?`<div class="chips">${r.matched.map(m=>`<span class="chip">${esc(m)}</span>`).join("")}</div>`:""}
 ${r.flags.map(f=>`<div class="flag">⚠ ${esc(f)}</div>`).join("")}</div>
 <div class="act"><button data-copy="${esc(r.k)}">Copy job description</button>
 <button data-done="${esc(r.k)}">${done[r.k]?"Undo":"Applied / skip"}</button></div></div>`).join("");
}
function extras(){
 const w=EXTRA.warnings,f=EXTRA.failed,a=document.getElementById("alert");
 if(w.length||f.length){a.innerHTML=`<div>${w.length?`<b>Needs attention:</b> these sources keep failing or went empty, so their jobs may be missing.<ul>${w.map(x=>`<li>${esc(x)}</li>`).join("")}</ul>Fix with <code>python run.py --check</code> and <code>python detect_ats.py</code>.`:""}
 ${f.length?`<details style="margin-top:${w.length?8:0}px;border:0;padding:0;background:none"><summary>${f.length} source(s) failed this run</summary><ul>${f.map(x=>`<li>${esc(x)}</li>`).join("")}</ul></details>`:""}</div>`}
 const s=EXTRA.suggestions;
 if(s.length)document.getElementById("sugg").innerHTML=`<details><summary>Companies hiring for your roles that aren't in your list yet (${s.length})</summary>
 <div class="mute" style="margin:6px 0">Seen on the job boards or LinkedIn. Add one so its jobs come straight from its careers page: <code>python detect_ats.py "Name" &lt;careers page link&gt;</code></div>
 <ul>${s.map(x=>`<li><b>${esc(x.name)}</b> · ${x.count} matching job${x.count>1?"s":""} seen<div class="mute">${x.titles.map(esc).join(" · ")}</div></li>`).join("")}</ul></details>`;
}
extras();
document.addEventListener("click",e=>{
 const c=e.target.dataset.copy,d=e.target.dataset.done;
 if(c){const r=DATA.find(x=>x.k===c);navigator.clipboard.writeText(r.jd).then(()=>{e.target.textContent="Copied";setTimeout(()=>e.target.textContent="Copy job description",1500)})}
 if(d){done[d]?delete done[d]:done[d]=1;save();render()}
});
["q","sort","hide"].forEach(id=>document.getElementById(id).addEventListener("input",render));
render();
</script></body></html>"""
