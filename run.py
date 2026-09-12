"""job-radar daily run: fetch → filter → dedupe → score → top5 → tailor → HTML."""
import html
import json
import os
import re
import sqlite3
import sys
from datetime import date
from pathlib import Path

import anthropic

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")  # scraped names can carry any unicode
except AttributeError:
    pass

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

NEGATIVE_TITLE_RE = re.compile(r"\b(coordinator|representative|instructor|tutor|trainer|freelance|hourly|intern)\b", re.I)
TECH_TRAINER_RE = re.compile(r"\btechnical\s+trainer\b", re.I)
SOFTWARE_ROLE_FAMILIES = {"technical_pm", "qa", "engineering", "solutions_implementation"}
TITLE_CAP = 30
INSTRUCTOR_TITLE_RE = re.compile(r"\b(instructor|teacher)\b", re.I)
BLOCKLIST_FILE = ROOT / "companies_blocked.txt"
BLOCKLIST_NOTES_FILE = ROOT / "companies_blocked_notes.txt"  # gitignored: private vetting notes
US_HQ_STRINGS = {"us", "usa", "u.s.", "u.s.a.", "united states", "united states of america"}
OFFER_DETAIL_FIELDS = ("ats_url", "ats_text", "post_date", "undated", "eu_tokens_absent",
                        "benefits_text", "location_text_source", "geo_verdict", "geo_evidence")
EVERGREEN_CAP = 40
UNDATED_CAP = 50
US_PROBABLE_CAP = 40


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


# Legacy fallback only — superseded by the Haiku-extracted years_matched field (see
# compute_score). Kept so rows scored before that field existed still get a years-gap check.
CAND_YEARS = {"pm_title": 1, "qa_dev": 3, "total": 4}
FAMILY_BONUS = {"product": 10, "technical_pm": 10, "qa": 10, "support_cs": 8, "solutions_implementation": 10,
                "business_analyst": 6, "engineering": -10, "other": -15}


def _excluded_countries():
    """Comma-separated list from EXCLUDED_COUNTRIES in .env. Empty by default."""
    import os
    return {c.strip().lower() for c in os.getenv("EXCLUDED_COUNTRIES", "").split(",") if c.strip()}


def _blocked_companies():
    """{lowercased name-or-substring: reason}. Both companies_blocked.txt and
    companies_blocked_notes.txt are gitignored (naming a company, even without a reason, isn't
    meant to be public) — see companies_blocked.example.txt for the format. Either file can hold
    'Company' or 'Company | reason' lines; notes.txt is just a place to add a reason for a
    company already listed (or not) in the main file, so the two merge, notes.txt winning on
    reason when both set one."""
    out = {}
    for path in (BLOCKLIST_FILE, BLOCKLIST_NOTES_FILE):
        if not path.exists():
            continue
        for line in path.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line or line.startswith("#"):
                continue
            if "|" in line:
                name, reason = line.split("|", 1)
                name, reason = name.strip().lower(), reason.strip()
            else:
                name, reason = line.lower(), ""
            out[name] = reason or out.get(name, "")
    return out


def _add_to_blocklist(company, reason=""):
    """Name goes in companies_blocked.txt; any reason goes in companies_blocked_notes.txt — both gitignored."""
    company = (company or "").strip()
    if not company or company.lower() in _blocked_companies():
        return
    with BLOCKLIST_FILE.open("a", encoding="utf-8") as fh:
        fh.write(company + "\n")
    if reason:
        with BLOCKLIST_NOTES_FILE.open("a", encoding="utf-8") as fh:
            fh.write(f"{company} | {reason}\n")


def _title_tokens(title):
    return set(re.findall(r"[a-z0-9]+", (title or "").lower()))


def _title_similarity(a, b):
    """Overlap coefficient (intersection / smaller set), not Jaccard: a repost mill often
    embellishes the same title ("QA Engineer" -> "AI-first QA Engineer"), so the shorter
    title being fully contained in the longer one should count as a match even though the
    union is large."""
    ta, tb = _title_tokens(a), _title_tokens(b)
    if not ta or not tb:
        return 0.0
    return len(ta & tb) / min(len(ta), len(tb))


def _detect_evergreen(jobs):
    """Same company posting near-identical titles (>80% token overlap) under separate
    Jobgether IDs — an evergreen/perpetually-reposted req, not one listing's own staleness
    (which post_date/STALE_DAYS_SOFT already covers). jobs: iterable of {"url","title","company"}."""
    from collections import defaultdict
    by_company, seen = defaultdict(list), set()
    for j in jobs:
        if j["url"] in seen or not j["company"].strip():
            continue
        seen.add(j["url"])
        by_company[j["company"].strip().lower()].append(j)
    flagged = set()
    for company, group in by_company.items():
        for i in range(len(group)):
            for k in range(i + 1, len(group)):
                if _title_similarity(group[i]["title"], group[k]["title"]) > 0.8:
                    flagged.add(group[i]["url"]); flagged.add(group[k]["url"])
    return flagged


def _detect_reposts(jobs):
    """Same title+company posted as separate Jobgether listings for DIFFERENT country labels
    (the Ireland/Spain/UK mass-distribution pattern). Must NOT fire when a listing simply moved
    ATS host (e.g. Greenhouse -> Ashby) with the same title, company, and country — that's a
    migration, not a repost, so the check is on remote_from, not on URL/host count."""
    from collections import defaultdict
    by_key = defaultdict(list)
    for j in jobs:
        by_key[(j["title"].strip().lower(), j["company"].strip().lower())].append(j)
    reposted_urls = set()
    for key, group in by_key.items():
        countries = {j.get("remote_from", "").strip().lower() for j in group}
        if len(group) > 1 and len(countries) > 1:
            reposted_urls.update(j["url"] for j in group)
    return reposted_urls


def _detect_gig_marketplaces(jobs):
    """Company with >5 concurrent listings, mostly 'X Instructor/Teacher - Remote' — tutoring mill pattern."""
    from collections import defaultdict
    by_company = defaultdict(list)
    for j in jobs:
        if j["company"].strip():
            by_company[j["company"].strip()].append(j)
    flagged = set()
    for company, group in by_company.items():
        if len(group) <= 5:
            continue
        matches = sum(1 for j in group if INSTRUCTOR_TITLE_RE.search(j["title"]))
        if matches / len(group) > 0.5:
            flagged.add(company)
    return flagged


STALE_DAYS = 180
STALE_DAYS_SOFT = 60
STALE_PENALTY = 20


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
    """Returns (hard_zero: bool, cap: int|None, penalty: int, flags: list[str])."""
    flags, hard, cap, penalty = [], False, None, 0
    country = (f.get("company_country") or "").strip().lower()
    if country and any(c in country for c in _excluded_countries()):
        flags.append(f"hq {country}"); hard = True
    if f.get("residency_restriction"):
        flags.append("residency restriction"); hard = True
    if f.get("local_market_product"):
        flags.append("single-market product"); cap = min(cap or 100, 40)
    if f.get("religious_or_political"):
        flags.append("religious/political mission"); cap = min(cap or 100, 30)

    company = (f.get("company") or "").strip().lower()
    blocked = _blocked_companies()
    hit = next((b for b in blocked if b in company), None) if company else None
    if hit:
        reason = blocked[hit]
        flags.append(f"blocked company: {f.get('company')}" + (f" ({reason})" if reason else "")); hard = True
    if f.get("gig_marketplace"):
        flags.append("gig/tutoring marketplace pattern"); hard = True

    if f.get("geo_verdict") == "us_only":
        flags.append(f"geo: {f.get('geo_evidence') or 'US-only'}"); hard = True
    elif f.get("geo_verdict") == "unknown" and (f.get("hq_country") or "").strip().lower() in US_HQ_STRINGS \
            and f.get("eu_tokens_absent"):
        # inference, not evidence — US HQ plus no EU/EMEA/UK/CET/GMT token anywhere in the text.
        # Capped, not zeroed, and deliberately doesn't touch the stored geo_verdict/geo_evidence
        # (those stay "unknown" — this is a separate, weaker signal).
        flags.append("geo: us_probable (US HQ, no EU/EMEA/UK/CET/GMT token found)"); cap = min(cap or 100, US_PROBABLE_CAP)

    if f.get("evergreen"):
        flags.append("evergreen repost (similar title, separate Jobgether ID)"); cap = min(cap or 100, EVERGREEN_CAP)

    title = f.get("title") or ""
    if title and not (TECH_TRAINER_RE.search(title) and f.get("role_family") in SOFTWARE_ROLE_FAMILIES):
        if NEGATIVE_TITLE_RE.search(title):
            flags.append("negative-list title"); cap = min(cap or 100, TITLE_CAP)

    if f.get("undated"):
        flags.append("no verifiable post date"); cap = min(cap or 100, UNDATED_CAP)
    else:
        age = _days_old(f.get("post_date") or f.get("posting_date"))
        if age is not None and age > STALE_DAYS:
            flags.append(f"posted {age}d ago"); cap = min(cap or 100, 40)
        elif age is not None and age > STALE_DAYS_SOFT:
            flags.append(f"posted {age}d ago (stale)"); penalty += STALE_PENALTY

    if f.get("repost_pattern"):
        flags.append("repost pattern (multi-country clone)"); penalty += STALE_PENALTY

    return hard, cap, penalty, flags


def compute_score(f):
    """Deterministic score from extracted facts. Every path that can return 0 or apply a cap
    appends a reason to f["flags"] first — including the ones that used to be silent (a job
    zeroed by e.g. language_blocker looked identical to one zeroed by nothing in particular).
    That visibility is what a false-negative audit actually needs to work from."""
    hard, cap, penalty, flags = red_flags(f)
    f["flags"] = flags
    if hard:
        return 0
    if not f.get("eligible_from_finland", True):
        flags.append("not eligible from Finland"); return 0
    if f.get("language_blocker"):
        flags.append("language_blocker"); return 0
    if f.get("timezone_blocker"):
        flags.append("timezone_blocker"); return 0
    if f.get("clearance_or_defence"):
        flags.append("clearance_or_defence"); return 0
    if f.get("seniority_title"):
        flags.append("seniority_title"); return 0
    floor = int(SALARY_FLOOR)
    sal = f.get("salary_eur_month")
    if sal and sal < floor:
        flags.append(f"salary {sal} < floor {floor}"); return 0
    yrs, scope = f.get("years_required_num"), f.get("years_scope")
    if scope == "total" and yrs and yrs >= 6:
        flags.append(f"years_required {yrs} (total) >= 6"); return 0
    total, met = max(int(f.get("requirements_total") or 0), 1), int(f.get("requirements_met") or 0)
    score = 45 + 45 * min(met / total, 1.0)                 # 45..90 from requirements match
    # years_matched is Haiku-extracted per posting (a single role's span, never summed across
    # overlapping roles — see score_cash.md). CAND_YEARS is the old static per-scope fallback,
    # kept only for rows scored before years_matched existed.
    matched = f.get("years_matched")
    if matched is None:
        matched = CAND_YEARS.get(scope)
    if yrs and matched is not None:
        gap = yrs - matched
        if gap >= 5:
            flags.append(f"years gap {gap} (required {yrs}, matched {matched})"); return 0
        if gap > 0:
            score -= 10 * gap
            flags.append(f"years gap {gap} (required {yrs}, matched {matched}, -{10*gap})")
    score += FAMILY_BONUS.get(f.get("role_family"), -15)
    if f.get("domain_required"):
        score -= 10
    if sal and sal >= floor:
        score += 8
    if f.get("outsourcing_employer"):
        score = min(score, 50)
    score -= penalty
    if cap is not None:
        score = min(score, cap)
    return int(max(0, min(100, round(score))))


PASSTHROUGH_FIELDS = ("title", "company", "geo_verdict", "geo_evidence", "ats_url", "ats_text", "post_date",
                      "benefits_text", "location_text_source", "repost_pattern", "gig_marketplace",
                      "evergreen", "undated", "eu_tokens_absent")


def score(job):
    user = f"Title: {job['title']}\nCompany: {job['company']}\nRemote from: {job['remote_from']}\nSkills: {', '.join(job['skills'])}\n\n{job.get('description','')[:6000]}"
    try:
        f = llm_json("claude-haiku-4-5-20251001", SCORE_PROMPT, user, 900)
        # deterministic fields we already extracted via scraping/regex win over any LLM guess
        for k in PASSTHROUGH_FIELDS:
            if job.get(k) not in (None, ""):
                f[k] = job[k]
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


EU_EEA_UK_COUNTRIES = {
    "austria", "belgium", "bulgaria", "croatia", "cyprus", "czech republic", "czechia", "denmark",
    "estonia", "finland", "france", "germany", "greece", "hungary", "ireland", "italy", "latvia",
    "liechtenstein", "lithuania", "luxembourg", "malta", "netherlands", "norway", "poland",
    "portugal", "romania", "slovakia", "slovenia", "spain", "sweden", "iceland",
    "united kingdom", "uk", "great britain",
}


def _looks_eu(f):
    country = f"{f.get('company_country') or ''} {f.get('hq_country') or ''}".lower()
    return f.get("geo_verdict") == "eu_ok" or any(c in country for c in EU_EEA_UK_COUNTRIES)


def _audit_rows(days=21, threshold=60):
    """Rows from the last `days` scoring below `threshold` whose company looks EU/EEA/UK,
    recomputed fresh (current rules, stored facts, no network/LLM calls). Returns
    {rule: [(url, first_seen, facts, score), ...]}."""
    from collections import defaultdict
    from datetime import date, timedelta
    cutoff = (date.today() - timedelta(days=days)).isoformat()
    con = db()
    rows = con.execute("SELECT url, first_seen, payload FROM jobs WHERE first_seen >= ?", (cutoff,)).fetchall()

    by_rule = defaultdict(list)
    for url, seen, payload in rows:
        j = json.loads(payload)
        f = dict(j.get("scoring") or {})
        f["title"], f["company"] = j.get("title", ""), j.get("company", "")
        score = compute_score(f)
        if score >= threshold or not _looks_eu(f):
            continue
        rule = f["flags"][0] if f["flags"] else "no rule fired (low fit / not extracted)"
        by_rule[rule].append((url, seen, f, score))
    return by_rule


def audit_false_negatives(days=21, threshold=60):
    """Candidates for a false-negative spot check — see _audit_rows()."""
    by_rule = _audit_rows(days, threshold)
    total = sum(len(v) for v in by_rule.values())
    print(f"{total} EU/EEA/UK rows from the last {days}d scoring below {threshold}, grouped by rule:\n")
    for rule, items in sorted(by_rule.items(), key=lambda kv: -len(kv[1])):
        print(f"=== {rule}  ({len(items)}) ===")
        for url, seen, f, score in items:
            print(f"  {score:>3}  {f['title'][:50]:<50} {f['company'][:25]:<25} seen {seen}")
            print(f"       all flags: {f.get('flags')}")
            print(f"       language_blocker={f.get('language_blocker')}  residency_restriction={f.get('residency_restriction')}"
                  f"  years_required={f.get('years_required')!r}  years_matched={f.get('years_matched')}")
            print(f"       {url}")
        print()


def recompute_report(n=30, dry_run=True):
    """Recompute compute_score() on the last n DB rows from stored facts only — no re-fetch, no LLM call."""
    con = db()
    rows = con.execute("SELECT url, payload FROM jobs ORDER BY first_seen DESC, rowid DESC LIMIT ?", (n,)).fetchall()
    print(f"{'old':>4} {'new':>4}  {'geo':<8} {'title':<45} {'company':<25} reason")
    for url, payload in rows:
        j = json.loads(payload)
        f = dict(j.get("scoring") or {})
        f["title"] = j.get("title", "")
        f["company"] = j.get("company", "")
        old_score = f.get("score", 0)
        new_score = compute_score(f)
        geo = f.get("geo_verdict", "unknown")
        reason = "; ".join(f.get("flags") or []) or "-"
        print(f"{old_score:>4} {new_score:>4}  {geo:<8} {f['title'][:45]:<45} {f['company'][:25]:<25} {reason}")
        if not dry_run and new_score != old_score:
            j["scoring"] = f
            j["scoring"]["score"] = new_score
            con.execute("UPDATE jobs SET score=?, payload=? WHERE url=?", (new_score, json.dumps(j), url))
    if dry_run:
        print("(dry run — nothing written; drop --dry-run to persist)")
    else:
        con.commit()
        print("applied.")


def reextract(urls, dry_run=False):
    """Re-run Haiku extraction only (no re-fetch, no scraping) on specific stored rows, using
    whatever prompt/compute_score is current. For prompt-only changes where the scraped
    description/ats_text hasn't changed and doesn't need to."""
    con = db()
    print(f"{'old':>4} {'new':>4}  title / company")
    for url in urls:
        row = con.execute("SELECT payload FROM jobs WHERE url=?", (url,)).fetchone()
        if not row:
            print(f"NOT FOUND: {url}")
            continue
        j = json.loads(row[0])
        old_score = (j.get("scoring") or {}).get("score", 0)
        j["scoring"] = score(j)
        new_score = j["scoring"]["score"]
        print(f"{old_score:>4} {new_score:>4}  {j.get('title', '')[:50]:<50} {j.get('company', '')[:25]}")
        if not dry_run:
            con.execute("UPDATE jobs SET score=?, payload=? WHERE url=?", (new_score, json.dumps(j), url))
            con.commit()
    if dry_run:
        print("(dry run — nothing written)")


def refetch(urls, dry_run=False):
    """Re-fetch specific stored URLs (offer page + ATS follow-through) and rescore with a fresh
    Haiku extraction. For verifying/fixing specific known rows, not a bulk operation."""
    from sources.jobgether import _offer_details
    con = db()
    print(f"{'old':>4} {'new':>4}  {'geo':<8} {'title':<45} {'company':<25} reason")
    for url in urls:
        row = con.execute("SELECT payload FROM jobs WHERE url=?", (url,)).fetchone()
        if not row:
            print(f"NOT FOUND: {url}")
            continue
        j = json.loads(row[0])
        old_score = (j.get("scoring") or {}).get("score", 0)
        details = _offer_details(url, j.get("title", ""), j.get("company", ""))
        j["description"] = details["description"]
        for k in OFFER_DETAIL_FIELDS:
            j[k] = details.get(k)
        j["scoring"] = score(j)
        new_score = j["scoring"]["score"]
        reason = "; ".join(j["scoring"].get("flags") or []) or "-"
        print(f"{old_score:>4} {new_score:>4}  {j['scoring'].get('geo_verdict','unknown'):<8} {j.get('title','')[:45]:<45} {j.get('company','')[:25]:<25} {reason}")
        if not dry_run:
            con.execute("UPDATE jobs SET score=?, payload=? WHERE url=?", (new_score, json.dumps(j), url))
            con.commit()
    if dry_run:
        print("(dry run — nothing written)")


def backfill_geo(min_score=50, dry_run=False):
    """For existing rows scoring >= min_score with no stored ats_text, follow the stored
    offer URL to its ATS page, populate ats_url/ats_text/post_date/geo_verdict, and rescore.
    No listing-page fetches — only the offer URLs already sitting in the DB.
    geo_verdict == "unknown" never changes a score: red_flags() only acts on "us_only"."""
    from sources.jobgether import _offer_details
    con = db()
    rows = con.execute("SELECT url, payload FROM jobs").fetchall()
    todo = []
    for url, payload in rows:
        j = json.loads(payload)
        f = j.get("scoring") or {}
        if f.get("score", 0) >= min_score and not f.get("ats_text"):
            todo.append((url, j))
    print(f"backfilling geo for {len(todo)} rows (score>={min_score}, no ats_text yet)")
    print(f"{'old':>4} {'new':>4}  {'geo':<8} {'title':<45} {'company':<25} evidence")
    for i, (url, j) in enumerate(todo, 1):
        f = dict(j.get("scoring") or {})
        old_score = f.get("score", 0)
        details = _offer_details(url, j.get("title", ""), j.get("company", ""))
        for k in OFFER_DETAIL_FIELDS:
            if details.get(k) not in (None, ""):
                f[k] = details[k]
                j[k] = details[k]  # also top-level: score()/reextract() only preserve fields
                                    # found here, since they always start from a fresh Haiku call
        f["title"] = j.get("title", "")
        f["company"] = j.get("company", "")
        new_score = compute_score(f)
        f["score"] = new_score
        j["scoring"] = f
        evidence = (f.get("geo_evidence") or "-")[:80]
        print(f"{old_score:>4} {new_score:>4}  {f.get('geo_verdict','unknown'):<8} {j.get('title','')[:45]:<45} {j.get('company','')[:25]:<25} {evidence}")
        if not dry_run:
            con.execute("UPDATE jobs SET score=?, payload=? WHERE url=?", (new_score, json.dumps(j), url))
            con.commit()
        if i % 10 == 0 or i == len(todo):
            print(f"...{i}/{len(todo)}", flush=True)
    if dry_run:
        print("(dry run — nothing written; drop --dry-run to persist)")
    else:
        print("applied.")


def main(dry_run=False):
    con = db()
    raw = []
    for s in SOURCES:
        try:
            raw += s.fetch()
        except Exception as e:
            print(f"[{s.name}] failed: {e}")
    print(f"fetched {len(raw)}")

    repost_urls = _detect_reposts(raw)
    gig_companies = _detect_gig_marketplaces(raw)
    # evergreen check spans the whole DB, not just this fetch, so a same-company/similar-title
    # pair split across two different runs (e.g. an old req vs. this week's repost) is still caught
    existing = [{"url": u, "title": json.loads(p).get("title", ""), "company": json.loads(p).get("company", "")}
                for u, p in con.execute("SELECT url, payload FROM jobs")]
    evergreen_urls = _detect_evergreen(raw + existing)
    for j in raw:
        j["repost_pattern"] = j["url"] in repost_urls
        j["gig_marketplace"] = j["company"].strip() in gig_companies
        j["evergreen"] = j["url"] in evergreen_urls
    if not dry_run:
        for c in gig_companies:
            _add_to_blocklist(c, reason="gig/tutoring marketplace pattern (>5 concurrent Instructor/Teacher listings)")

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
        if not dry_run:
            con.execute("INSERT OR IGNORE INTO jobs (url, first_seen, score, payload) VALUES (?,?,?,?)",
                        (j["url"], date.today().isoformat(), j["scoring"]["score"], json.dumps(j)))
            con.commit()
        if i % 10 == 0 or i == len(new):
            print(f"scored {i}/{len(new)}", flush=True)

    if dry_run:
        print("\n--dry-run: no DB writes. Verdicts:")
        for j in new:
            s = j["scoring"]
            print(f"{s.get('score',0):>3}  geo={s.get('geo_verdict','?'):<7}  {j['title'][:50]:<50} {j['company'][:25]:<25} {'; '.join(s.get('flags') or [])}")
        return

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
    dry = "--dry-run" in sys.argv
    if "--rescore" in sys.argv:
        rescore()
    elif "--recompute" in sys.argv:
        recompute_report(n=30, dry_run=dry)
    elif "--refetch" in sys.argv:
        idx = sys.argv.index("--refetch")
        urls = [a for a in sys.argv[idx + 1:] if not a.startswith("--")]
        refetch(urls, dry_run=dry)
    elif "--reextract" in sys.argv:
        idx = sys.argv.index("--reextract")
        urls = [a for a in sys.argv[idx + 1:] if not a.startswith("--")]
        reextract(urls, dry_run=dry)
    elif "--backfill-geo" in sys.argv:
        min_score = 50
        if "--min-score" in sys.argv:
            min_score = int(sys.argv[sys.argv.index("--min-score") + 1])
        backfill_geo(min_score=min_score, dry_run=dry)
    elif "--audit-negatives" in sys.argv:
        days = int(sys.argv[sys.argv.index("--days") + 1]) if "--days" in sys.argv else 21
        threshold = int(sys.argv[sys.argv.index("--threshold") + 1]) if "--threshold" in sys.argv else 60
        audit_false_negatives(days=days, threshold=threshold)
    else:
        main(dry_run=dry)
