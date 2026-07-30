import json
from pathlib import Path


source = Path(".demo_data/summary.json")
output = Path(".demo_data/report.txt")
summary = json.loads(source.read_text(encoding="utf-8"))
lines = ["Demo report", *[f"{name}: {value}" for name, value in sorted(summary.items())]]
output.write_text("\n".join(lines) + "\n", encoding="utf-8")
print(f"published demo report to {output}")
