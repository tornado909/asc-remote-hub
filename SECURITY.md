# Security policy

Report vulnerabilities privately through GitHub Security Advisories / private vulnerability reporting. Never post access tokens, files or trace logs in public issues.

Supported: latest release in the 1.x series.

## Threat model

The public HTTPS hub, its owner and OS administrators are trusted. This is NOT end-to-end encryption against the hub operator. A server administrator can technically inspect traffic and database files. An authenticated user can access only devices belonging to that user; the OAuth authorization-code/PKCE flow binds tokens to users and resources.

Device owners choose read-only, write/exec, or GUI permissions. Command execution is not sandboxed by allowed file roots: an executable can read anything permitted to its operating-system user. Use dedicated least-privilege accounts and keep sensitive devices in separate VMs if necessary.

## Operational hardening

- Enforce TLS with a valid certificate, HSTS and a firewall restricting the internal HTTP port to the reverse proxy.
- Do not run agents as root or a privileged user unnecessarily.
- Disable full traffic tracing by default. If enabled, logs may contain complete files, personal details, terminal outputs and screenshots.
- Never upload configuration, OAuth keys, device credentials, SQLite databases, archives with private data or logs to GitHub.
- Patch the host and Docker; use strong account passwords; add rate/bot protection for public registration.
- Windows secrets are protected using user-scoped DPAPI; Linux secrets are stored under file mode 0600.
- The SQLite error history retains limited diagnostic metadata; optional full JSON traces are for a single explicitly configured user and stored in private directories.
- Terminating tracing does not delete previous logs. Purge traces separately after verifying tests.

## Current limitations

Agent background process handles may not survive an agent restart. The server's in-memory queue is not durable across a hub restart. Heavy commands may block subsequent jobs for that device. Do not blindly retry side-effecting commands on timeout.

## Secret scanning before publishing

Each push and pull request runs **two independent free checks**:

- **Gitleaks** (pinned release and verified SHA-256) scans the complete reachable
  Git history and the working tree for known credentials, with redacted output.
- **Public-tree privacy guard** checks all tracked files (including tests and the
  guard itself) for non-example email addresses, URLs and bare domain names,
  RFC1918 addresses, private key material, and unexpected runtime files.

GitHub's native secret scanning is automatically available free for public
repositories. For a user-owned private repository it may not be available
without a qualifying enterprise account or additional subscription. The
Gitleaks CI checks run without purchasing GitHub Secret Protection.

Before any public release, review the checks and scan externally with
independent tools. Static checks are not proof of zero leakage: encrypted,
fragmented, newly issued or unusual credentials may go undetected. If you
need to reject deployment-specific identifiers that do not match the generic
rules, supply a **private, newline-separated** ASC_RELEASE_PRIVATE_LITERALS
environment variable to the guard, without committing its values.
