# Security policy

Dag Runner executes commands from workflow YAML with the permissions of the
service account. Treat workflow files as trusted code.

- Do not expose the web console directly to the public internet.
- Put authentication and TLS in a reverse proxy when remote access is needed.
- Do not store credentials in workflow YAML or logs.
- Run the service with the least OS and filesystem privileges required.

Please report security issues privately to the repository maintainers rather
than opening a public issue with exploit details.
