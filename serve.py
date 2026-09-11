"""Local UI: python serve.py -> http://127.0.0.1:5000
Lists scored jobs from jobs.db, button generates CV block + cover letter on demand (Sonnet), saves to DB."""
import html, json, sqlite3
from flask import Flask, request, redirect
from run import DB, tailor  # reuses .env loading, prompts, client

app = Flask(__name__)
CSS = """body{font:15px/1.45 system-ui;max-width:960px;margin:2rem auto;padding:0 1rem;color:#222}
.card{border:1px solid #ddd;border-radius:8px;padding:1rem;margin:1rem 0}.head{display:flex;gap:1rem;align-items:flex-start}
.score{font-size:1.6rem;font-weight:700;min-width:3rem;color:#3D3B5C}h2{margin:0;font-size:1.1rem}.meta{color:#666;font-size:.9rem}
pre{white-space:pre-wrap;background:#f7f7f7;padding:.7rem;border-radius:6px}details{margin:.5rem 0}summary{cursor:pointer}
button{background:#3D3B5C;color:#fff;border:0;padding:.4rem .8rem;border-radius:6px;cursor:pointer}.sm{background:#888}
.bar{display:flex;gap:1rem;align-items:center;margin-bottom:1rem}input[type=number]{width:5rem}"""


# status is new / shortlisted / applied / rejected. Nothing in run.py's fetch/rescore/backfill
# writes this column (they only ever touch score+payload) — it is user-set only, via /status.
def ensure_status():
    con = sqlite3.connect(DB)
    cols = [r[1] for r in con.execute("PRAGMA table_info(jobs)")]
    if "status" not in cols:
        con.execute("ALTER TABLE jobs ADD COLUMN status TEXT DEFAULT 'new'")
    con.execute("UPDATE jobs SET status='new' WHERE status='' OR status IS NULL")
    con.execute("UPDATE jobs SET status='rejected' WHERE status='skip'")  # one-time vocabulary migration
    con.commit()


def rows(min_score, days, view):
    ensure_status()
    con = sqlite3.connect(DB)
    q = "SELECT url, first_seen, score, payload, tailored, status FROM jobs ORDER BY score DESC"
    out = []
    for url, seen, score, payload, tailored, status in con.execute(q):
        j = json.loads(payload); j["tailored"] = json.loads(tailored) if tailored else None
        j["first_seen"] = seen; j["status"] = status or "new"; j["score_col"] = score
        out.append(j)

    if view == "shortlist":
        # Ignores min_score AND fetch date on purpose: rescoring may change a shortlisted
        # row's number, and its fetch date only gets staler, but it must stay visible here.
        return sorted((j for j in out if j["status"] == "shortlisted"), key=lambda j: -j["score_col"])

    out = [j for j in out if j["score_col"] >= min_score]
    if view == "open":
        out = [j for j in out if j["status"] not in ("applied", "rejected", "shortlisted")]
    elif view == "applied":
        out = [j for j in out if j["status"] == "applied"]
    # view == "all": no status filter beyond min_score

    seen_days = sorted({j["first_seen"] for j in out}, reverse=True)[:days]
    return [j for j in out if j["first_seen"] in seen_days]


def card(j, i):
    e = html.escape; s = j["scoring"]; t = j.get("tailored")
    body = ""
    if t:
        body = f"""<details open><summary>CV: title + profile + skill order</summary><p><b>{e(t.get('cv_title',''))}</b></p>
<pre>{e(t.get('cv_profile',''))}</pre><p>{e(', '.join(t.get('cv_skills_top8',[])))}</p></details>
<details open><summary>Cover letter</summary><pre id="cl{i}">{e(t.get('cover_letter',''))}</pre>
<button class="sm" onclick="navigator.clipboard.writeText(document.getElementById('cl{i}').innerText)">Copy</button>
<form method="post" action="/tailor" style="display:inline"><input type="hidden" name="url" value="{e(j['url'])}"><button class="sm">Regenerate</button></form></details>"""
    else:
        body = f"""<form method="post" action="/tailor"><input type="hidden" name="url" value="{e(j['url'])}">
<button>Generate CV + cover letter</button></form>"""
    status = j.get("status", "new")
    buttons = "".join(f'<button name="status" value="{v}" class="sm">{label}</button> '
                       for v, label in (("shortlisted", "Shortlist"), ("applied", "Applied"),
                                         ("rejected", "Reject"), ("new", "Reopen")) if v != status)
    return f"""<div class="card"><div class="head"><span class="score">{s['score']}</span><div>
<h2><a href="{e(j['url'])}" target="_blank">{e(j['title'])}</a></h2>
<div class="meta">{e(j['company'])} · {e(j['remote_from'])} · {e(j.get('contract',''))} · {e(str(s.get('salary') or 'salary not shown'))} · years req: {e(str(s.get('years_required') or '?'))} · flags: {e(', '.join(s.get('flags') or []) or 'none')} · seen {e(j['first_seen'])} · status: {e(status)}</div></div></div>
<p>{e(s.get('reason',''))}</p>{body}
<p><form method="post" action="/status" style="display:inline"><input type="hidden" name="url" value="{e(j['url'])}">
{buttons}</form></p></div>"""


@app.get("/")
def index():
    min_score = int(request.args.get("min", 60)); days = int(request.args.get("days", 1)); view = request.args.get("view", "open")
    jobs = rows(min_score, days, view)
    cards = "".join(card(j, i) for i, j in enumerate(jobs))
    sel = lambda v: "selected" if v == view else ""
    return f"""<!doctype html><meta charset="utf-8"><title>job-radar jobs</title><style>{CSS}</style>
<form class="bar">Min score <input type="number" name="min" value="{min_score}"> Last <input type="number" name="days" value="{days}"> run-days
<select name="view"><option value="open" {sel('open')}>Open</option><option value="shortlist" {sel('shortlist')}>Shortlist</option><option value="applied" {sel('applied')}>Applied</option><option value="all" {sel('all')}>All</option></select>
<button>Show</button> <span>{len(jobs)} jobs</span></form>{cards or '<p>Nothing here.</p>'}"""


@app.post("/status")
def set_status():
    ensure_status()
    con = sqlite3.connect(DB)
    con.execute("UPDATE jobs SET status=? WHERE url=?", (request.form.get("status", "new"), request.form["url"])); con.commit()
    return redirect(request.referrer or "/")


@app.post("/tailor")
def do_tailor():
    url = request.form["url"]
    con = sqlite3.connect(DB)
    payload = con.execute("SELECT payload FROM jobs WHERE url=?", (url,)).fetchone()[0]
    j = json.loads(payload)
    t = tailor(j)
    con.execute("UPDATE jobs SET tailored=? WHERE url=?", (json.dumps(t), url)); con.commit()
    return redirect(request.referrer or "/")


if __name__ == "__main__":
    app.run(port=5000, debug=False)
