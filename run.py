"""Cash-mode daily run: fetch → filter → dedupe → score → top5 → tailor → HTML."""
import html
import json
import os
import re
import sqlite3
from datetime import date
from pathlib import Path

import anthropic

from sources.jobgether import JobgetherSource

ROOT = Path(__file__).parent

# load .env (KEY=value per line) into the environment
_env = ROOT / ".env"
if _env.exists():
    for line in _env.read_text().splitlines():
        if "=" in line and not line.strip().startswith("#"):
            k, v = line.split("=", 1)
            os.environ.setdefault(k.strip(), v.strip())
DB = ROOT / "jobs.db"
OUT = ROOT / "out"
SALARY_FLOOR = os.environ.get("SALARY_FLOOR", "4000")
TOP_N = 5
SOURCES = [JobgetherSource()]  # add your existing sources here

client = anthropic.Anthropic(timeout=60.0, max_retries=2)
FACTS = (ROOT / "prompts/candidate_facts.md").read_text(encoding="utf-8").strip()
SCORE_PROMPT = (ROOT / "prompts/score_cash.md").read_text(encoding="utf-8").replace("{{SALARY_FLOOR}}", SALARY_FLOOR).replace("{{CANDIDATE_FACTS}}", FACTS)
TAILOR_PROMPT = (ROOT / "prompts/tailor.md").read_text(encoding="utf-8")

SENIOR = re.compile(r"\b(senior|staff|principal|lead|head of|director|vp|chief)\b", re.I)
US_ONLY = re.compile(r"\b(US only|U\.S\. only|United States only|must be (located|based) in the (US|United States))\b", re.I)


def pre_filter(job):
    if SENIOR.search(job["title"]):
        return False
    if US_ONLY.search(job.get("description", "")):
        return False
    return True


def db():
    con = sqlite3.connect(DB)
    con.execute("CREATE TABLE IF NOT EXISTS jobs (url TEXT PRIMARY KEY, first_seen TEXT, score INT, payload TEXT, tailored TEXT)")
    return con


def llm_json(model, system, user, max_tokens=800):
    msg = client.messages.create(model=model, max_tokens=max_tokens, system=system,
                                 messages=[{"role": "user", "content": user}])
    text = msg.content[0].text
    start = text.find("{")
    if start < 0:
        raise ValueError(f"no JSON in reply: {text[:120]}")
    obj, _ = json.JSONDecoder().raw_decode(text[start:])  # first JSON object only, ignore trailing prose
    return obj


CAND_YEARS = {"pm_title": 1, "qa_dev": 3, "total": 4}
FAMILY_BONUS = {"product": 10, "technical_pm": 10, "qa": 10, "support_cs": 8, "solutions_implementation": 10,
                "business_analyst": 6, "engineering": -10, "other": -15}


def _excluded_countries():
    """Comma-separated list from EXCLUDED_COUNTRIES in .env. Empty by default."""
    import os
    return {c.strip().lower() for c in os.getenv("EXCLUDED_COUNTRIES", "").split(",") if c.strip()}


STALE_DAYS = 180


def _days_old(d):
    """days since a YYYY-MM-DD / YYYY-MM string, or None."""
    from datetime import date
    if not d:
        return None
    try:
        parts = [int(x) for x in str(d).split("-")]
        if len(parts) == 2:
            parts.append(1)
        return (date.today() - date(*parts[:3])).days
    except Exception:
        return None


def red_flags(f):
    """Returns (hard_zero: bool, cap: int|None, flags: list[str])."""
    flags, hard, cap = [], False, None
    country = (f.get("company_country") or "").strip().lower()
    if country and any(c in country for c in _excluded_countries()):
        flags.append(f"hq {country}"); hard = True
    if f.get("residency_restriction"):
        flags.append("residency restriction"); hard = True
    if f.get("local_market_product"):
        flags.append("single-market product"); cap = min(cap or 100, 40)
    if f.get("religious_or_political"):
        flags.append("religious/political mission"); cap = min(cap or 100, 30)
    age = _days_old(f.get("posting_date"))
    if age is not None and age > STALE_DAYS:
        flags.append(f"posted {age}d ago"); cap = min(cap or 100, 40)
    return hard, cap, flags


def compute_score(f):
    """Deterministic score from extracted facts."""
    hard, cap, flags = red_flags(f)
    f["flags"] = flags
    if hard:
        return 0
    if (not f.get("eligible_from_finland", True) or f.get("language_blocker") or f.get("timezone_blocker")
            or f.get("clearance_or_defence") or f.get("seniority_title")):
        return 0
    floor = int(SALARY_FLOOR)
    sal = f.get("salary_eur_month")
    if sal and sal < floor:
        return 0
    yrs, scope = f.get("years_required_num"), f.get("years_scope")
    if scope == "total" and yrs and yrs >= 6:
        return 0
    total, met = max(int(f.get("requirements_total") or 0), 1), int(f.get("requirements_met") or 0)
    score = 45 + 45 * min(met / total, 1.0)                 # 45..90 from requirements match
    if yrs and scope in CAND_YEARS:
        gap = yrs - CAND_YEARS[scope]
        if gap >= 5:
            return 0
        if gap > 0:
            score -= 10 * gap
    score += FAMILY_BONUS.get(f.get("role_family"), -15)
    if f.get("domain_required"):
        score -= 10
    if sal and sal >= floor:
        score += 8
    if f.get("outsourcing_employer"):
        score = min(score, 50)
    if cap is not None:
        score = min(score, cap)
    return int(max(0, min(100, round(score))))


def score(job):
    user = f"Title: {job['title']}\nCompany: {job['company']}\nRemote from: {job['remote_from']}\nSkills: {', '.join(job['skills'])}\n\n{job.get('description','')[:6000]}"
    try:
        f = llm_json("claude-haiku-4-5-20251001", SCORE_PROMPT, user, 500)
        f["score"] = compute_score(f)
        return f
    except Exception as e:
        return {"score": 0, "reason": f"score error: {e}", "role_family": "", "salary": None, "language": "en"}


def tailor(job):
    p = (TAILOR_PROMPT.replace("{{CANDIDATE_FACTS}}", FACTS).replace("{{TITLE}}", job["title"])
         .replace("{{COMPANY}}", job["company"]).replace("{{REMOTE_FROM}}", job["remote_from"])
         .replace("{{DESCRIPTION}}", job.get("description", "")[:7000]))
    try:
        return llm_json("claude-sonnet-4-6", "Output JSON only.", p, 900)
    except Exception as e:
        return {"cv_title": "", "cv_profile": f"tailor error: {e}", "cv_skills_top8": [], "cover_letter": ""}


def card(j):
    s = j["scoring"]; t = j.get("tailored", {})
    e = html.escape
    skills = ", ".join(t.get("cv_skills_top8", []))
    return f"""
<div class="card">
  <div class="head"><span class="score">{s['score']}</span>
    <div><h2><a href="{e(j['url'])}" target="_blank">{e(j['title'])}</a></h2>
    <div class="meta">{e(j['company'])} · {e(j['remote_from'])} · {e(j['contract'])} · {e(str(s.get('salary') or 'salary not shown'))}</div></div></div>
  <p class="reason">{e(s.get('reason',''))}</p>
  <details><summary>CV: title + profile + skill order</summary>
    <p><b>{e(t.get('cv_title',''))}</b></p><pre>{e(t.get('cv_profile',''))}</pre><p>{e(skills)}</p></details>
  <details><summary>Cover letter</summary>
    <pre id="cl-{abs(hash(j['url']))}">{e(t.get('cover_letter',''))}</pre>
    <button onclick="navigator.clipboard.writeText(document.getElementById('cl-{abs(hash(j['url']))}').innerText)">Copy</button></details>
</div>"""


def render(top, de, others, day):
    css = """body{font:15px/1.45 system-ui;max-width:900px;margin:2rem auto;padding:0 1rem;color:#222}
.card{border:1px solid #ddd;border-radius:8px;padding:1rem;margin:1rem 0}.head{display:flex;gap:1rem;align-items:flex-start}
.score{font-size:1.6rem;font-weight:700;min-width:3rem;color:#3D3B5C}h2{margin:0;font-size:1.1rem}.meta{color:#666;font-size:.9rem}
pre{white-space:pre-wrap;background:#f7f7f7;padding:.7rem;border-radius:6px}details{margin:.5rem 0}summary{cursor:pointer}
table{border-collapse:collapse;width:100%}td{padding:.3rem .5rem;border-bottom:1px solid #eee;font-size:.9rem}"""
    rows = "".join(f"<tr><td>{j['scoring']['score']}</td><td><a href='{html.escape(j['url'])}' target='_blank'>{html.escape(j['title'])}</a></td><td>{html.escape(j['company'])}</td><td>{html.escape(j['remote_from'])}</td></tr>" for j in others)
    return f"""<!doctype html><meta charset="utf-8"><title>Jobs {day}</title><style>{css}</style>
<h1>Top {len(top)} — {day}</h1>{''.join(card(j) for j in top)}
{'<h1>German-language roles</h1>' + ''.join(card(j) for j in de) if de else ''}
<h1>Also scored (review pile)</h1><table>{rows}</table>"""


def rescore():
    """Re-score rows whose scoring failed, using the stored payload (no refetch)."""
    con = db()
    rows = con.execute("SELECT url, payload FROM jobs").fetchall()
    import sys
    todo = [(u, json.loads(p)) for u, p in rows
            if "--all" in sys.argv or str(json.loads(p).get("scoring", {}).get("reason", "")).startswith("score error")]
    print(f"rescoring {len(todo)}")
    for i, (u, j) in enumerate(todo, 1):
        j["scoring"] = score(j)
        con.execute("UPDATE jobs SET score=?, payload=? WHERE url=?", (j["scoring"]["score"], json.dumps(j), u))
        con.commit()
        if i % 10 == 0 or i == len(todo):
            print(f"rescored {i}/{len(todo)}", flush=True)


def main():
    con = db()
    raw = []
    for s in SOURCES:
        try:
            raw += s.fetch()
        except Exception as e:
            print(f"[{s.name}] failed: {e}")
    print(f"fetched {len(raw)}")

    new = []
    for j in raw:
        if not pre_filter(j):
            continue
        if con.execute("SELECT 1 FROM jobs WHERE url=?", (j["url"],)).fetchone():
            continue
        new.append(j)
    print(f"new after filter {len(new)}")

    for i, j in enumerate(new, 1):
        j["scoring"] = score(j)
        con.execute("INSERT OR IGNORE INTO jobs (url, first_seen, score, payload) VALUES (?,?,?,?)",
                    (j["url"], date.today().isoformat(), j["scoring"]["score"], json.dumps(j)))
        con.commit()
        if i % 10 == 0 or i == len(new):
            print(f"scored {i}/{len(new)}", flush=True)

    scored = sorted(new, key=lambda j: -j["scoring"]["score"])
    en = [j for j in scored if j["scoring"].get("language") != "de" and j["scoring"]["score"] >= 60]
    de = [j for j in scored if j["scoring"].get("language") == "de" and j["scoring"]["score"] >= 60]
    OUT.mkdir(exist_ok=True)
    day = date.today().isoformat()
    path = OUT / f"{day}.html"
    path.write_text(render(en[:TOP_N], de[:2], en[TOP_N:20], day), encoding="utf-8")
    print(f"static report: {path}")
    print("now run: python serve.py  and open http://127.0.0.1:5000")


if __name__ == "__main__":
    import sys
    rescore() if "--rescore" in sys.argv else main()
