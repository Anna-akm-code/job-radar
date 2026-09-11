"""Jobgether source. Server-rendered HTML, no login needed for listings.

Selectors are inferred from page structure — verify once with test_source.py and adjust.
"""
import html as _html
import re
import time
from datetime import datetime
from pathlib import Path
from bs4 import BeautifulSoup

try:
    from curl_cffi import requests as _req  # browser TLS fingerprint, gets past bot checks
    _IMPERSONATE = {"impersonate": "chrome"}
except ImportError:  # fallback, may 403
    import requests as _req
    _IMPERSONATE = {}

BASE = "https://jobgether.com"
HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/128.0.0.0 Safari/537.36",
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "en-US,en;q=0.9",
    "Referer": "https://jobgether.com/remote-jobs",
}

ROLES = [
    # product
    "product-owner", "product-analyst", "product-manager-tech", "product-delivery",
    "technical-product-manager", "technical-program-manager", "technical-project-manager",
    # customer-facing technical
    "customer-solutions-engineer", "sales-engineer", "implementation-engineer",
    "technical-account-manager", "technology-integration-consultant",
    "integration-engineer", "it-integration-engineer", "integration-specialist",
    "api-integration-specialist",
    # QA
    "qa-engineer", "qa-tester", "test-automation-engineer",
    "software-development-engineer-in-test-sdet",
]
CATEGORIES = [
    "product", "support-and-technical-support", "quality-assurance-and-testing", "technical-sales",
]
SKILLS = [
    "product-management", "product-knowledge", "product-strategy", "product-analysis",
    "project-implementation", "program-implementation", "test-automation", "unit-testing",
    "technical-writing", "account-management",
    "scrum-software-development", "user-story", "cypress", "postman", "rest-apis",
    "java", "postgresql", "quality-assurance", "business-analysis",
]
EXPERIENCE = "experience=junior-1-2-years&experience=mid-level-2-5-years"
LOCATIONS = ["finland"]  # returns Remote from Anywhere + Europe
ELIGIBLE = ("finland", "europe", "emea", "worldwide", "anywhere", "nordic")
PAGES = 2
DEBUG_DUMPED = []

# ---------- ATS follow-through + deterministic geo/date extraction ----------
# Jobgether's own "Remote from" label is unreliable (US roles get labelled "Anywhere").
# We follow the original apply link and regex the source text for hard US-only signals
# instead of trusting Jobgether's label or any LLM judgment call.

APPLY_URL_RE = re.compile(r'data-apply-url="([^"]+)"')
DATE_POSTED_RE = re.compile(r'"datePosted"\s*:\s*"([^"]+)"')
MATCH_SCORE_LINE_RE = re.compile(r"^.*\bmatch score\b.*$\n?", re.I | re.M)

US_HARD_PATTERNS = [
    re.compile(r"remote\s*-\s*us\b", re.I),
    re.compile(r"remote\s*\(\s*united states\s*\)", re.I),
    re.compile(r"\bus[-\s]based\b", re.I),
    re.compile(r"\bunited states only\b", re.I),
    re.compile(r"must be (?:located|based) in the (?:us|united states)\b", re.I),
    re.compile(r"\b401\s*\(?k\)?\b", re.I),
]
US_TZ_RE = re.compile(r"\b(est|pst)\s+hours\b", re.I)
EU_TZ_HINT_RE = re.compile(r"\b(cet|cest|gmt|utc|bst|european time|eu time ?zone)\b", re.I)
BENEFITS_US_ONLY_RE = re.compile(r"\b(medical|dental|vision)\b[\s\S]{0,300}?\b401\s*\(?k\)?\b", re.I)
# Deliberately excludes "worldwide"/"anywhere" — that's Jobgether's own unreliable default
# remote-from label (the whole reason this feature exists), not evidence of real EU eligibility.
EU_TOKEN_RE = re.compile(r"\b(eu|european union|europe|emea|uk|cet|gmt)\b", re.I)
US_STATES = ["alabama", "alaska", "arizona", "arkansas", "california", "colorado", "connecticut",
             "delaware", "florida", "georgia", "hawaii", "idaho", "illinois", "indiana", "iowa",
             "kansas", "kentucky", "louisiana", "maine", "maryland", "massachusetts", "michigan",
             "minnesota", "mississippi", "missouri", "montana", "nebraska", "nevada",
             "new hampshire", "new jersey", "new mexico", "new york", "north carolina",
             "north dakota", "ohio", "oklahoma", "oregon", "pennsylvania", "rhode island",
             "south carolina", "south dakota", "tennessee", "texas", "utah", "vermont",
             "virginia", "washington", "west virginia", "wisconsin", "wyoming"]
US_STATE_LIST_RE = re.compile(r"(?:" + "|".join(US_STATES) + r")(?:\s*,\s*(?:" + "|".join(US_STATES) + r")){2,}", re.I)
BENEFITS_SECTION_RE = re.compile(r"(benefits?|perks?)\s*[:\-][\s\S]{0,400}", re.I)
LOCATION_SECTION_RE = re.compile(r"(location|remote from|work location|eligib\w*)\s*[:\-][\s\S]{0,200}", re.I)
JS_EMPTY_MARKERS = ("enable javascript",)
RELATIVE_DAYS_RE = re.compile(r"(\d+)\+?\s*days?\s+ago", re.I)


def _strip_match_score(text):
    """Drop any 'match score' line — that's Jobgether's own fit-% badge, not the posting."""
    return MATCH_SCORE_LINE_RE.sub("", text)


def _strip_key_facts_card(soup):
    """Jobgether's 'Key facts' sidebar is a tag list (location/contract/seniority/role/LANGUAGE
    tag) with no sentence structure — read as prose it misleads the extractor into treating a
    taxonomy tag (e.g. a 'Dutch' category tag) as a stated hard requirement. Drop the whole card;
    remote_from/contract are already scraped separately from the listing card."""
    for node in soup.find_all(string=lambda s: s and s.strip().lower() == "key facts"):
        card = node.find_parent("div")
        if card:
            card.decompose()
    return soup


def _apply_url(raw_html):
    m = APPLY_URL_RE.search(raw_html)
    return _html.unescape(m.group(1)) if m else None


def _parse_date(raw):
    raw = (raw or "").strip()
    for length, fmt in ((24, "%a %b %d %Y %H:%M:%S"), (19, "%Y-%m-%dT%H:%M:%S"), (10, "%Y-%m-%d")):
        try:
            return datetime.strptime(raw[:length], fmt).date().isoformat()
        except ValueError:
            continue
    return None


def _json_ld_date(raw_html):
    m = DATE_POSTED_RE.search(raw_html)
    return _parse_date(m.group(1)) if m else None


def _relative_date(text):
    """'9 days ago' / '30+ days ago' -> an ISO date. For '30+' this is a floor (at least this
    old), which is exactly what we want when it disagrees with a structured date — see _older."""
    m = RELATIVE_DAYS_RE.search(text or "")
    if not m:
        return None
    from datetime import date, timedelta
    return (date.today() - timedelta(days=int(m.group(1)))).isoformat()


def _older(*dates):
    """The oldest of any given ISO dates (None entries ignored). A repost bump can make a
    structured datePosted look newer than reality; when a relative-text date disagrees, the
    older one is the honest one."""
    real = [d for d in dates if d]
    return min(real) if real else None


def _geo_evidence(text):
    hits = []
    for pat in US_HARD_PATTERNS:
        m = pat.search(text)
        if m:
            hits.append(m.group(0))
    for m in US_TZ_RE.finditer(text):
        window = text[max(0, m.start() - 120):m.end() + 120]
        if not EU_TZ_HINT_RE.search(window):
            hits.append(m.group(0))
    m = BENEFITS_US_ONLY_RE.search(text)
    if m and not EU_TOKEN_RE.search(text):
        hits.append(m.group(0)[:150])
    m = US_STATE_LIST_RE.search(text)
    if m:
        hits.append(m.group(0)[:150])
    return hits


def geo_verdict(ats_text, location_text=""):
    """Deterministic eu_ok / us_only / unknown from source text. No LLM judgment."""
    blob = f"{location_text}\n{ats_text or ''}"
    hits = _geo_evidence(blob)
    if hits:
        evidence = re.sub(r"\s+", " ", "; ".join(hits)).strip()  # a hit can span a real newline
        return "us_only", evidence[:400]
    if not (ats_text or "").strip():
        return "unknown", ""
    if EU_TOKEN_RE.search(blob):
        return "eu_ok", ""
    return "unknown", ""


def _looks_js_only(text):
    low = text.lower()
    return any(marker in low for marker in JS_EMPTY_MARKERS) or len(text.strip()) < 200


def _web_search_fallback(title, company):
    """No-API-key fallback for JS-only ATS pages: DuckDuckGo HTML result snippets."""
    q = f'"{title}" "{company}" 2026'
    try:
        r = _req.get("https://html.duckduckgo.com/html/", params={"q": q}, headers=HEADERS,
                     timeout=20, **_IMPERSONATE)
        if r.status_code != 200:
            return ""
        soup = BeautifulSoup(r.text, "html.parser")
        snippets = [el.get_text(" ", strip=True) for el in soup.select(".result__snippet")]
        return " ".join(snippets)[:2000]
    except Exception:
        return ""


def _benefits_snippet(text):
    m = BENEFITS_SECTION_RE.search(text or "")
    return m.group(0)[:400] if m else ""


def _location_snippet(text):
    m = LOCATION_SECTION_RE.search(text or "")
    return m.group(0)[:200] if m else ""


def _fetch_ats(offer_html, title, company, description=""):
    """Follow the original ATS link from a Jobgether offer page and pull source text.
    Jobgether frequently mirrors the real posting body into its own page (benefits blocks,
    401(k) mentions, etc. show up verbatim there even when the ATS itself is a JS-only SPA
    we can't read) — so the geo check runs over the Jobgether description too, not only
    whatever text the ATS page adds."""
    apply_url = _apply_url(offer_html)
    # "keep the older of the two": Jobgether's JSON-LD can look newer than reality after a
    # repost bump, but its own visible "N+ days ago" text is a floor on the true age.
    post_date = _older(_json_ld_date(offer_html), _relative_date(description))
    ats_text = ""
    if apply_url:
        try:
            r = _req.get(apply_url, headers=HEADERS, timeout=20, **_IMPERSONATE)
            time.sleep(1.0)
            if r.status_code == 200:
                asoup = BeautifulSoup(r.text, "html.parser")
                for tag in asoup.select("nav, footer, header, script, style"):
                    tag.decompose()
                ats_text = asoup.get_text("\n", strip=True)
                if _looks_js_only(ats_text):
                    ats_text = _web_search_fallback(title, company)
                post_date = _older(post_date, _json_ld_date(r.text), _relative_date(ats_text))
        except Exception:
            ats_text = ""
    combined = f"{description}\n{ats_text}"
    verdict, evidence = geo_verdict(combined)
    return {
        "ats_url": apply_url,
        "ats_text": ats_text[:4000],
        "post_date": post_date,
        "undated": post_date is None,
        "eu_tokens_absent": not EU_TOKEN_RE.search(combined),
        "benefits_text": _benefits_snippet(ats_text) or _benefits_snippet(description),
        "location_text_source": _location_snippet(ats_text) or _location_snippet(description),
        "geo_verdict": verdict,
        "geo_evidence": evidence,
    }


def _get(url):
    r = _req.get(url, headers=HEADERS, timeout=30, **_IMPERSONATE)
    if r.status_code != 200:
        raise RuntimeError(f"HTTP {r.status_code}")
    time.sleep(1.0)  # be polite
    return BeautifulSoup(r.text, "html.parser")


def _parse_cards(soup):
    """Each listing has an <a href="/offer/..."> title link; walk up to its card."""
    jobs = []
    seen = set()
    for a in soup.select('a[href*="/offer/"]'):
        url = a["href"]
        if not url.startswith("http"):
            url = BASE + url
        if url in seen:
            continue
        seen.add(url)
        card = a
        for _ in range(8):  # climb to the card container
            if card.parent is None:
                break
            card = card.parent
            if "Remote from" in card.get_text(" "):
                break
        text = card.get_text(" ", strip=True)
        m = re.search(r"Remote from\s+(.+?)(?:Full time|Part time|Internships|Freelance|Fixed term|Apply Now|$)", text)
        remote_from = m.group(1).strip() if m else ""
        comp = card.select_one('a[href*="company-"], a[href*="company="]')
        contract = card.select_one('a[href*="contractType="]')
        skills = [s.get_text(strip=True) for s in card.select('a[href*="skill="], a[href*="/remote-jobs/"]')
                  if s.get_text(strip=True) and s is not comp]
        jobs.append({
            "source": "jobgether",
            "title": a.get_text(strip=True),
            "company": comp.get_text(strip=True) if comp else "",
            "url": url,
            "remote_from": remote_from,
            "contract": contract.get_text(strip=True) if contract else "",
            "skills": skills[:6],
        })
    return jobs


def _eligible(job):
    rf = job["remote_from"].lower()
    return any(k in rf for k in ELIGIBLE)


def _offer_details(url, title, company):
    """One fetch of the Jobgether offer page: description text + ATS follow-through."""
    try:
        r = _req.get(url, headers=HEADERS, timeout=30, **_IMPERSONATE)
        time.sleep(1.0)
        if r.status_code != 200:
            raise RuntimeError(f"HTTP {r.status_code}")
    except Exception:
        return {"description": "", "ats_url": None, "post_date": None, "benefits_text": "",
                "location_text_source": "", "geo_verdict": "unknown", "geo_evidence": ""}
    raw = r.text
    soup = BeautifulSoup(raw, "html.parser")
    main = soup.select_one("main") or soup
    for tag in main.select("nav, footer, header, script, style"):
        tag.decompose()
    _strip_key_facts_card(main)
    description = _strip_match_score(main.get_text("\n", strip=True))[:8000]
    return {"description": description, **_fetch_ats(raw, title, company, description)}


def fetch(max_jobs=200, with_description=True):
    urls = []
    for loc in LOCATIONS:
        L = f"location={loc}"
        for r in ROLES:
            urls.append(f"{BASE}/search-offers?role={r}&{EXPERIENCE}&{L}&sort=date")
        for c in CATEGORIES:
            urls.append(f"{BASE}/search-offers?category={c}&{EXPERIENCE}&{L}&sort=date")
        for sk in SKILLS:
            urls.append(f"{BASE}/search-offers?skill={sk}&{EXPERIENCE}&{L}&sort=date")

    jobs, seen = [], set()
    for u in urls:
        try:
            soup = _get(u)
        except Exception as e:
            print(f"[jobgether] skip {u}: {e}")
            continue
        cards = _parse_cards(soup)
        kept = 0
        for j in cards:
            if j["url"] in seen or not _eligible(j):
                continue
            seen.add(j["url"])
            jobs.append(j)
            kept += 1
        sample = cards[0]["remote_from"] if cards else "-"
        print(f"[jobgether] {u.split('.com/')[1]}: parsed {len(cards)}, kept {kept}, sample remote_from='{sample}'")
        if not DEBUG_DUMPED:
            (Path("out") / "debug_page.html").write_text(str(soup), encoding="utf-8")
            DEBUG_DUMPED.append(u)
            if len(jobs) >= max_jobs:
                break
        if len(jobs) >= max_jobs:
            break

    if with_description:
        for j in jobs:
            j.update(_offer_details(j["url"], j["title"], j["company"]))
    return jobs


class JobgetherSource:
    name = "jobgether"

    def fetch(self):
        return fetch()
