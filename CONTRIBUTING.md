# Contributing

Contributions, documentation, accessibility and compatibility improvements are welcome. Use an issue before major architecture changes.

Run tests:

    python3 -m unittest discover -s tests -q
    node --check server/static/common.js
    node --check server/static/auth.js
    node --check server/static/dashboard.js
    node --check server/installer_cli/connect.cjs

Include regression tests for bug fixes. Changes to OAuth/tenant isolation must test at least two user accounts. CLI changes should be validated with a fresh npm cache and --dry-run on Windows and Linux.

Do not commit local secrets, .env, user databases, generated trace logs, snapshots or private screenshots. For vulnerabilities, use the private reporting guidance in SECURITY.md.

The project is independent and not affiliated with OpenAI or Desktop Commander. Respect third-party licenses and marks.

## Local secret checks

Install a trusted Gitleaks release and run:

    gitleaks git --no-banner --redact=100 --exit-code 1 .
    gitleaks dir --no-banner --redact=100 --exit-code 1 .
    python3 scripts/check_public_tree.py

CI installs an exact version and validates its SHA-256 before scanning.
Do not add file-wide or test-wide allowlists to suppress findings; replace
synthetic test values instead. Do not upload reports containing actual secrets.
