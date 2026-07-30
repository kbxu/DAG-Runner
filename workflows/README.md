# Workflows

Place production workflow YAML files in this directory. The web service and CLI
use `./workflows` by default when started from the repository root.

Start from a file in [`../demo/workflows`](../demo/workflows), then review its
commands, working directory, environment setup, timezone, and schedule before
enabling it.

Do not commit secrets. Inject them through the service account environment or a
secret manager.
