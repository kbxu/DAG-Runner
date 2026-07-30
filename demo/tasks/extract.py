import json
from pathlib import Path


output = Path(".demo_data/raw.json")
output.parent.mkdir(parents=True, exist_ok=True)
records = [
    {"category": "alpha", "value": 12},
    {"category": "beta", "value": 8},
    {"category": "alpha", "value": 5},
]
output.write_text(json.dumps(records, indent=2), encoding="utf-8")
print(f"extracted {len(records)} demo records")
