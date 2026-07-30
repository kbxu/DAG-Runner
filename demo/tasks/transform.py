import json
from pathlib import Path


source = Path(".demo_data/raw.json")
output = Path(".demo_data/clean.json")
records = json.loads(source.read_text(encoding="utf-8"))
clean = [record for record in records if record["value"] >= 0]
output.write_text(json.dumps(clean, indent=2), encoding="utf-8")
print(f"transformed {len(clean)} demo records")
