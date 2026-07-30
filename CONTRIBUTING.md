# Contributing

Contributions are welcome through issues and pull requests.

## Development setup

```bash
python -m venv .venv
.venv/bin/pip install -r requirements.txt
.venv/bin/python -m unittest discover -s tests -v
```

On Windows, replace `.venv/bin/python` with `.venv\Scripts\python.exe`.

Please keep the runner lightweight, avoid executing real workflow commands in
tests, and add focused tests for behavior changes. Use fake executors or mocks
for process execution.
