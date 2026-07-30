import json
from collections import defaultdict
from pathlib import Path


source = Path(".demo_data/clean.json")
output = Path(".demo_data/summary.json")
totals = defaultdict(int)
for record in json.loads(source.read_text(encoding="utf-8")):
    totals[record["category"]] += record["value"]
output.write_text(json.dumps(dict(totals), indent=2), encoding="utf-8")
print(f"summarized {len(totals)} categories")
