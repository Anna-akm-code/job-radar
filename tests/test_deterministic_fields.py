"""Regression test for the 2026-09-12 false-negative-audit bug: backfill_geo() wrote
geo_verdict/ats_url/etc. into a row's facts dict but never onto the job dict's top level, so a
later score() call — which always starts from a fresh Haiku response and only preserves a
deterministic field if it finds it at the job dict's TOP LEVEL — silently dropped it back to
nothing. reextract() is just a thin DB read/write around score(), so this exercises the exact
code path that lost the data.

Run: python tests/test_deterministic_fields.py
"""
import sys
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import run

# Fields score() must treat as authoritative — never replaced by anything Haiku says, even
# when Haiku's own schema happens to include a field of the same name (post_date, ats_url).
DETERMINISTIC_FIELDS = {
    "geo_verdict": "us_only",
    "geo_evidence": "401(k)",
    "ats_url": "https://example.invalid/ats/12345",
    "post_date": "2026-01-15",
    "benefits_text": "Medical, dental, vision, 401(k).",
    "location_text_source": "Remote - US",
    "undated": False,
    "eu_tokens_absent": True,
}

# A fresh Haiku response that deliberately conflicts on the two overlapping field names
# (post_date, ats_url) — the real bug: these came back as None/wrong and won.
FAKE_HAIKU_RESPONSE = {
    "eligible_from_finland": True, "language_blocker": False, "timezone_blocker": False,
    "clearance_or_defence": False, "seniority_title": False,
    "years_required": None, "years_required_num": None, "years_scope": None, "years_matched": None,
    "role_family": "qa", "requirements_total": 5, "requirements_met": 3,
    "domain_required": False, "outsourcing_employer": False,
    "salary": None, "salary_eur_month": None, "language": "en", "misleading_title": False,
    "residency_restriction": False, "company_country": None, "local_market_product": False,
    "religious_or_political": False, "posting_date": None,
    "post_date": "1999-01-01",          # conflicts with DETERMINISTIC_FIELDS on purpose
    "ats_url": None,                    # conflicts with DETERMINISTIC_FIELDS on purpose
    "hq_country": None,
    "reason": "fake haiku response for testing",
}


def test_score_preserves_deterministic_fields():
    """A job dict that already carries geo_verdict/ats_url/post_date (as it does after a real
    fetch or refetch) must come out of score() with those exact values, unchanged, no matter
    what a fresh Haiku call returns."""
    job = {
        "title": "QA Engineer", "company": "Example Co", "remote_from": "Anywhere",
        "skills": [], "description": "some description text",
        **DETERMINISTIC_FIELDS,
    }
    with patch.object(run, "llm_json", return_value=dict(FAKE_HAIKU_RESPONSE)):
        result = run.score(job)

    failures = [f"{k}: expected {v!r}, got {result.get(k)!r}"
                for k, v in DETERMINISTIC_FIELDS.items() if result.get(k) != v]
    return failures


def main():
    failures = test_score_preserves_deterministic_fields()
    if failures:
        print("FAILED — deterministic fields were overwritten by a fresh Haiku response:")
        for f in failures:
            print(" -", f)
        sys.exit(1)
    print("ok: all deterministic fields survived a fresh Haiku call unchanged")


if __name__ == "__main__":
    main()
