# Contributing

Contributions, documentation, accessibility and compatibility improvements are welcome. Use an issue before major architecture changes.

Run tests:

    python3 -m unittest discover -s tests -q
    node --check server/static/app.js
    node --check server/installer_cli/connect.cjs

Include regression tests for bug fixes. Changes to OAuth/tenant isolation must test at least two user accounts. CLI changes should be validated with a fresh npm cache and --dry-run on Windows and Linux.

Do not commit local secrets, .env, user databases, generated trace logs, snapshots or private screenshots. For vulnerabilities, use the private reporting guidance in SECURITY.md.

The project is independent and not affiliated with OpenAI or Desktop Commander. Respect third-party licenses and marks.
