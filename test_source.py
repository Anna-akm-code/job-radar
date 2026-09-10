"""python test_source.py --source jobgether  → writes out/test_jobgether.json for inspection."""
import argparse, json, importlib
from pathlib import Path
p = argparse.ArgumentParser(); p.add_argument("--source", required=True); p.add_argument("--no-desc", action="store_true")
a = p.parse_args()
mod = importlib.import_module(f"sources.{a.source}")
jobs = mod.fetch(max_jobs=40, with_description=not a.no_desc)
Path("out").mkdir(exist_ok=True)
Path(f"out/test_{a.source}.json").write_text(json.dumps(jobs, indent=2, ensure_ascii=False))
print(f"{len(jobs)} jobs → out/test_{a.source}.json")
for j in jobs[:5]: print(j["title"], "|", j["company"], "|", j["remote_from"])
