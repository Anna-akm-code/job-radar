"""Jobgether source. Server-rendered HTML, no login needed for listings.

Selectors are inferred from page structure — verify once with test_source.py and adjust.
"""
import re
import time
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


def _description(url):
    try:
        soup = _get(url)
    except Exception:
        return ""
    main = soup.select_one("main") or soup
    # drop nav/footer noise
    for tag in main.select("nav, footer, header, script, style"):
        tag.decompose()
    return main.get_text("\n", strip=True)[:8000]


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
            j["description"] = _description(j["url"])
    return jobs


class JobgetherSource:
    name = "jobgether"

    def fetch(self):
        return fetch()
