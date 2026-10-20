## 2026-10-21 · Hub 1.2.3 / Agent and CLI 1.1.3

- Added locally confirmed `unpair` and `uninstall` commands on Windows/Linux through the existing domain-specific `npx .../cli.tgz` installer.
- Unpair now revokes precisely the current device's credential on the hub; uninstall also stops the dedicated service/task and removes owned local files. `--local-only` supports migration from an unavailable hub; `--dry-run` previews changes.
- Added both copyable commands to the public hub website and new Windows tray menu items. The existing website Unpair action clearly states that it revokes server access only.
- Tenant-bound `/agent/unpair` checks the device's bearer credentials, and tests cover cross-tenant revocation, local cleanup and degraded-network behavior.

## Agent 1.1.2 — 2026-10-19

- New lightweight ASC GUI Engine for isolated Win32 Desktops (no VM, additional OS, virtual monitor, or HID driver).
- Seven MCP tools: gui_desktop_create, gui_desktop_sessions, gui_desktop_launch, gui_desktop_windows, gui_desktop_action, gui_desktop_capture, gui_desktop_close.
- Launch via CreateProcessW lpDesktop, strict owned PID/HWND checks, standard Edit/Button control actions, PrintWindow capture.
- Windows Job Object ensures hidden GUI process trees terminate on session close or agent crash. Max three Desktops per agent.
- Normal user mouse/focus untouched in native Windows integration tests; unsupported graphics/game input remain explicitly unverified.
- Tests and design notes: tests/test_isolated_desktop_tools.py, tests/test_isolated_desktop_windows.py and docs/ISOLATED_GUI.md.

## Hub server 1.2.0 — 2026-10-15

- Provider-neutral MCP OAuth dynamic client registration (RFC 7591).
- Secure HTTPS callbacks for web applications and loopback HTTP callbacks with explicit ports for desktop applications.
- PKCE S256, exact redirect matching, resource-bound tokens and multi-tenant isolation preserved.
- Generic OAuth consent shows requested callback host and unverified client name.
- OAuth applications dashboard with per-account token revocation.
- Existing ChatGPT OAuth registrations/tokens migrated without invalidation.
- Hub 1.2.0 is compatible with current agent and installer release 1.1.0.
- Client ID Metadata Documents not yet supported; DCR remains enabled.

## 1.1.0 — 2026-10-11

- Four concurrent outbound-agent worker slots with separately serialized side effects and GUI actions; lease TTL suppresses late starts after server timeout.
- Windows window_list, window_focus, window_screenshot, terminal_send with verified HWND/PID/foreground; tested with the real Codex Windows Terminal on Dekim-PC.
- Batched read_files, stat_paths, bounded directory_tree, literal search_text within permitted roots.
- Per-user latency metrics (queue_ms, agent_ms, relay_ms, total_ms) via /api/performance and MCP performance_stats; 7-day retention.
- Persistent serialized SQLite connection; WAL/FULL durability for account credentials, pairing and revocation; WAL/NORMAL only for disposable heartbeat/performance/usage writes.
- Full traffic tracing remains enabled when requested; large payloads compressed losslessly with SHA-256 integrity and decoding in error traces.
- Agent UTF-8 with BOM support preserved; package 1.1.0 downloadable via stable /cli.tgz plus versioned fallback.
- Backward-compatible agent protocol for previously installed 1.0.3/1.0.4 devices.

### Not yet part of 1.1.0

- Durable reconstruction of OS process handles after an agent restart (cannot be guaranteed solely from a PID).
- Portable terminal_read for hidden ConPTY sessions; use window_screenshot and UI automation in this release.
- Windows Job Objects for killing subprocess trees and blob-store deduplication beyond inline compression.

# Changelog

## 1.0.3 — 2026-10-11

- Preserve Windows GUI dependencies for existing paired devices when upgrading from a manually installed agent to the persistent tray launcher.
- Still uses the constant install URL `npx --yes https://hub.example.com/cli.tgz`.

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
