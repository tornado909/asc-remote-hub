# ASC Remote Hub

**Self-hosted, multi-user MCP gateway for ChatGPT and your own Windows/Linux computers.**

Run commands, inspect and edit files, manage background processes, and (on an interactive Windows desktop) operate the screen from ChatGPT, through infrastructure you control — **without a paid Desktop Commander relay or an OpenAI Platform tunnel**.

[Русский README](README.ru.md) · [Deployment guide](docs/DEPLOYMENT.md) · [Security model](SECURITY.md)

> [!IMPORTANT]
> ASC Remote Hub is an independent community project, not affiliated with OpenAI, ChatGPT, or Desktop Commander. It imposes **no per-tool-call quota**, but your ChatGPT plan and its MCP capabilities may still have limits. Server hosting, networking, and software dependencies are your responsibility.

## How it works

```text
ChatGPT ── HTTPS + OAuth ──► Your ASC Remote Hub ── outbound HTTPS polling ──► Your Windows / Linux agents
                                │
                        SQLite (per-user data)
```

Agents initiate connections to your gateway, so they do not require inbound ports, publicly accessible desktop IPs, or third-party relays. A public HTTPS endpoint is required so ChatGPT can reach the gateway.

## Features

- **One-command installation** from your own domain: `npx --yes https://YOUR_DOMAIN/cli-1.0.0.tgz`. Windows uses `npx.cmd`.
- **Free registration** and account-separated devices, tokens and usage statistics.
- **OAuth 2.1 authorization-code + PKCE** for the ChatGPT MCP connection; each token belongs to a specific account and MCP resource.
- **File operations:** enumerate, search, read, create, replace exact text, inspect and move within permitted roots.
- **Processes:** execute programs without an implicit shell; launch jobs, read bounded output and stop jobs.
- **Desktop controls (optional):** screen capture, keyboard, mouse and scrolling where an interactive graphical session is available.
- **Per-account error history:** collapsible dashboard, device/tool filters, reason and timestamp.
- **Server-rendered account pages:** signed-in HTML has no login form or login script, so refreshes do not flash authentication UI.
- **Opt-in diagnostics:** full request/response tracing for one explicitly selected account, disabled by default.
- **Outbound-only agents**, heartbeat during long-running tasks, and an exclusive lock preventing duplicate agents using the same identity.
- **No database server or paid external runtime dependencies:** stdlib Python, SQLite, Docker, optional reverse proxy.

## Quick start: host your own server

Requires a Linux server with Docker Compose, a domain you own and a valid HTTPS certificate.

```bash
git clone https://github.com/tornado909/asc-remote-hub.git
cd asc-remote-hub
cp .env.example .env
```

Edit `.env`:

```dotenv
ASC_HUB_URL=https://hub.example.com
ASC_HUB_DOMAIN=hub.example.com
ASC_BIND_IP=127.0.0.1
ASC_BIND_PORT=18765
PUID=1000
PGID=1000
ASC_HUB_TRACE_PAYLOADS=0
```

Create the persistent data directory and start the service:

```bash
mkdir -p data
chmod 700 data
docker compose up -d --build hub
```

Place it behind a trusted HTTPS reverse proxy forwarding to `127.0.0.1:18765`. Or use the optional Caddy profile when ports 80/443 are free:

```bash
docker compose --profile public up -d --build
```

The `ASC_HUB_URL` value controls OAuth discovery, callback links, downloadable agent packages and the command displayed on your hub website. **No source-code editing is needed to change the domain.** See [deployment details](docs/DEPLOYMENT.md).

## Connect a computer

Open `https://hub.example.com`, create an account and click **Add device** to obtain a one-time pairing code. On the computer you want to connect, run the command displayed by that hub:

| Operating system | Command |
| --- | --- |
| Windows (PowerShell) | `npx.cmd --yes https://hub.example.com/cli-1.0.0.tgz` |
| Linux | `npx --yes https://hub.example.com/cli-1.0.0.tgz` |

The CLI requires **Node.js 18+**, **Python 3.10+** and `tar`. Node.js is only used for installation; the running agent uses Python. It verifies the SHA-256 of the download, prompts for a one-time code, asks for the allowed folders and permissions and configures autostart.

Safe installation preview on a computer with no setup changes:

```bash
npx --yes https://hub.example.com/cli-1.0.0.tgz --dry-run
```

By default only read-only file operations are allowed. Write access, command execution and (on Windows) GUI control require explicit selection. Do not enable full access on devices that hold sensitive workloads unless you understand the risks.

## Add to ChatGPT

In ChatGPT, add a custom MCP server with the address:

```text
https://hub.example.com/mcp
```

Select OAuth and authorize access using **your own** hub account. The consent page is served by your hub. Feature availability depends on your ChatGPT plan and whether the client supports the required MCP tools.

## Privacy and security

- User A cannot list or control user B's registered devices or view their log history.
- Device credentials are unique; Windows secrets are encrypted for the local user using DPAPI, Linux files use permissions `0600`.
- TLS is mandatory outside tests. Content/authentication tokens are not included in ordinary application logs.
- Full payload tracing is **OFF by default**, and must be enabled explicitly for a single account. It may include private file content and screenshots — handle it as sensitive data.
- A server administrator with root access can technically inspect traffic and files on that server. This is **not end-to-end encrypted from the server operator**.
- This is remote execution software. Deploy it only on devices you own or are authorized to administer; protect your OAuth accounts, server, DNS and TLS keys.

Read the complete [security policy and threat model](SECURITY.md).

## Development

```bash
python3 -m unittest discover -s tests -q
node --check server/static/common.js
node --check server/static/auth.js
node --check server/static/dashboard.js
node --check server/installer_cli/connect.cjs
```

Tests include tenant isolation, OAuth/PKCE, invalid-path handling, persistence of counters, trace privacy, cross-platform agent behavior, package generation for two different domains, and HTTP integration tests.

## Current limitations

A background process job ID belongs to the current agent runtime; jobs started before an agent restart cannot always be resumed via `read_process`. Long operations may be serialized per device. Native Windows GUI interaction requires an active user desktop session. File transfer tools are intentionally bounded; for large transfers, use an appropriate authenticated file-transfer channel.

See [architecture and operational limits](docs/ARCHITECTURE.md).

## Contributing

See [CONTRIBUTING.md](CONTRIBUTING.md). Vulnerabilities should be reported privately as described in [SECURITY.md](SECURITY.md).

## License

[MIT](LICENSE). Open source, self-hosted; no forced subscription or artificial tool-call ceiling.
