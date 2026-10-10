# Changelog

## 1.0.2 — 2026-10-11

- Linux installer accepts direct root sessions and installs a root-owned systemd unit; systemd availability is validated before redeeming a single-use code.
- Windows agent uses a silent notification-area supervisor with Open hub / Restart agent / Exit controls, instead of a persistent PowerShell console.
- Keeps the stable install URL `npx --yes https://hub.example.com/cli.tgz`. Existing device credentials are preserved.


## 1.0.1 — 2026-10-10

- Windows agent encrypts new device credentials via native user-bound DPAPI, without PowerShell subprocess piping.
- The npx CLI now bundles the SHA-256 verified agent archive, so local installs do not require a second download.
- Credential store is checked before redeeming a single-use pairing code, preventing the observed orphan registration on DPAPI failure.
- The cross-platform `npx` installer resumes interrupted installations, preserves existing credentials and repairs background startup.
- Improve transient HTTPS download error reporting and retry; existing agent tokens remain compatible.
- This is a client/installer release; users still need to revoke any orphan device created by 1.0.0 failures.


## 1.0.3 — 2026-10-10

- Expand the privacy guard to detect non-example bare domains in common DNS zones, internal hostnames and private LAN IPs.
- Add regression tests using disposable Git worktrees, including private reference detection.
- Add free, SHA-256-verified Gitleaks in GitHub Actions for full-history and worktree scanning with redacted diagnostics.
- Replace test fixtures that resembled genuine credentials without suppressing security rules.
- Explain free CI scanning versus GitHub Secret Protection for private repositories.

## Security maintenance — 2026-10-10

- Removed deployment-specific identifiers from public tests and release checks.
- Replaced literal-based exclusions with generic checks for real email addresses, non-example origins, private artifacts, and key material.
- Added regression tests ensuring the release guard checks test files and itself.

## 1.0.2 — 2026-10-10

- Render guest login and signed-in dashboard as separate server-selected HTML responses.
- Remove all login/registration form markup and login JavaScript from authenticated page responses; no first-paint flash or hidden form in the DOM.
- Login and logout now reload the correct page via the server, preserving session cookies, OAuth and CSRF checks.
- Retain the compact, scrollable error history; add HTTP and browser-behavior regression tests.
- No database migration or agent protocol update; 1.0.0 agents remain compatible.

## 1.0.1 — 2026-10-10

- Fixed the expanded error history layout: cards keep their full height inside a scrolling list.
- Improved the collapsed/expanded section label.
- Added regression tests for the dashboard overflow behavior.
- No database, OAuth or agent protocol changes; agent/installer package version remains 1.0.0.

## 1.0.0 — 2026-10-10

First public release of ASC Remote Hub:

- Self-hosted multi-user MCP gateway and OAuth 2.1/PKCE.
- User-owned Windows and Linux agents, outbound-only HTTPS connections and one-command onboarding.
- Deployment-specific canonical domain from ASC_HUB_URL and auto-generated SHA-256 verified installer archives.
- File, command, process and optional Windows GUI tools.
- Exclusive agent lock and heartbeat during long-running commands.
- Compact expandable per-account error dashboard and opt-in full tracing.
- Standalone documentation, security policy and test suite.

Documented current limitations: non-durable server queue and background job handles lost after agent restart.
