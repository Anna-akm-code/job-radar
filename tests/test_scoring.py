"""Regression gate for compute_score(). Run before/after any scoring change:
    python tests/test_scoring.py

Loads tests/golden_set.json — hand-labelled real postings, each storing the extracted
facts dict as of the date it was labelled (not the score; the score is always recomputed
against the CURRENT compute_score() so this catches regressions in either direction).
A "positive" must land at or above APPLY_THRESHOLD; a "negative" must land below it.
A row may set "known_gap": true (with "known_gap_reason") for a negative we can't yet
prove from scraped text alone (e.g. a JS-only ATS with no usable fallback text) — those
print as warnings, not failures, until there's real evidence to encode as a rule.
"""
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import run
from run import compute_score

APPLY_THRESHOLD = 60  # matches the >=60 cutoff run.py uses to put a job in the daily digest
GOLDEN_SET = Path(__file__).parent / "golden_set.json"


def _score(facts):
    """Rows tagged blocklist_fixture use a synthetic company name (the real blocklisted
    company is redacted from this public fixture) — inject it into a throwaway blocklist so
    the actual hard-zero code path still gets exercised, then restore the real one."""
    if facts.get("reason") != "blocklist_fixture":
        return compute_score(facts)
    real_blocklist = run._blocked_companies
    run._blocked_companies = lambda: {facts["company"].strip().lower(): "blocklist_fixture"}
    try:
        return compute_score(facts)
    finally:
        run._blocked_companies = real_blocklist


def main():
    rows = json.loads(GOLDEN_SET.read_text(encoding="utf-8"))
    failures, warnings = [], []
    print(f"{'label':<9} {'score':>5}  {'want':<6} name")
    for row in rows:
        facts = dict(row["facts"])  # compute_score mutates (adds "flags") — never touch the fixture
        score = _score(facts)
        is_positive = row["label"] == "positive"
        ok = (score >= APPLY_THRESHOLD) if is_positive else (score < APPLY_THRESHOLD)
        want = f">={APPLY_THRESHOLD}" if is_positive else f"<{APPLY_THRESHOLD}"
        status = "ok" if ok else ("GAP" if row.get("known_gap") else "FAIL")
        print(f"{row['label']:<9} {score:>5}  {want:<6} {row['name']}  [{status}]")
        if not ok:
            detail = f"{row['name']} ({row['label']}): scored {score}, wanted {want} — flags={facts.get('flags')}"
            if row.get("known_gap"):
                warnings.append(detail + f" — known gap: {row.get('known_gap_reason', '')}")
            else:
                failures.append(detail)

    if warnings:
        print("\nknown gaps (not blocking, but unresolved):")
        for w in warnings:
            print(" -", w)

    if failures:
        print("\nFAILED:")
        for f in failures:
            print(" -", f)
        sys.exit(1)
    print("\nall golden rows OK")


if __name__ == "__main__":
    main()
