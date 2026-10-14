<div align="center">
  <img src="server/static/brand-mark.png" width="86" alt="ASC Remote Hub logo">

# ASC Remote Hub

**One self-hosted gateway. Your computers. Any compatible MCP client.**

Control your Windows and Linux machines from AI agents through infrastructure you own.
No third-party desktop relay or vendor-specific MCP lock-in.

[**Get started**](#quick-start) · [**Connect an MCP client**](#connect-an-mcp-client) · [**Security**](#security) · [**Русский**](README.ru.md)

![MCP](https://img.shields.io/badge/MCP-Streamable_HTTP-396A96?style=flat-square) ![OAuth](https://img.shields.io/badge/OAuth-PKCE_%2B_DCR-396A96?style=flat-square) ![Platform](https://img.shields.io/badge/agents-Windows_%7C_Linux-396A96?style=flat-square) [![MIT](https://img.shields.io/badge/license-MIT-238636?style=flat-square)](LICENSE)

</div>

> [!NOTE]
> **Independent open-source project.** Not affiliated with OpenAI, Cursor, OpenMausBot, or Desktop Commander. Your MCP client may have its own usage limits; the Hub imposes no per-tool-call quota.

## Overview

ASC Remote Hub is a **multi-user Model Context Protocol (MCP) gateway** that connects AI assistants to computers you own or administer. It exposes a standard remote **Streamable HTTP** MCP endpoint, handles OAuth authorization, and forwards approved tool calls to lightweight Windows and Linux agents.

The server is self-hosted with Docker and SQLite. Agents establish **outbound HTTPS connections** to the Hub, so you do not need to expose RDP, SSH, or an inbound agent port to the internet.

### What you can do

| Capability | Details |
| --- | --- |
| **Work with files** | List directories, read multiple files, inspect metadata, search content, create and modify files within configured roots. |
| **Run commands** | Execute local programs, manage background jobs, collect bounded output, and check system information. |
| **Control the desktop** | On an interactive Windows session, inspect windows, capture screenshots, focus a chosen window and send verified terminal input. |
| **Use different AI clients** | Connect ChatGPT, Cursor, OpenMausBot, or another compatible MCP application using OAuth. |
| **Keep users separate** | Per-account devices, OAuth tokens, permissions, activity counters, and error history. |
| **Inspect performance** | Latency breakdown, diagnostics, and optional full traffic tracing. |

## Architecture

```text
   ChatGPT     Cursor     OpenMausBot     Other MCP clients
      \          |           |               /
       \_________|___________|______________/
                         |
              Streamable HTTP + OAuth
                         |
                  +----------------+
                  | ASC Remote Hub |
                  | Python + SQLite|
                  | Per-user access|
                  +----------------+
                   ^              ^
           outbound HTTPS    outbound HTTPS
                   |              |
             Windows agent    Linux agent
             files / shell    files / shell
             desktop UI       background jobs
```

The Hub checks the OAuth identity and device ownership before forwarding each call. Agents enforce their locally configured capabilities and file roots. No special ChatGPT transport or OpenAI-hosted relay is required.

## Quick start

You need a Linux host with **Docker Compose**, a domain, and an HTTPS reverse proxy or the optional Caddy configuration.

**1. Clone and configure:**

```bash
git clone https://github.com/tornado909/asc-remote-hub.git
cd asc-remote-hub
cp .env.example .env
```

Set at least these values in `.env` (replace `hub.example.com` with your own host):

```dotenv
ASC_HUB_URL=https://hub.example.com
ASC_HUB_DOMAIN=hub.example.com
ASC_BIND_IP=127.0.0.1
ASC_BIND_PORT=18765
PUID=1000
PGID=1000
ASC_HUB_TRACE_PAYLOADS=0
```

**2. Start the Hub:**

```bash
mkdir -p data
chmod 700 data
docker compose up -d --build hub
```

Proxy HTTPS traffic to `127.0.0.1:18765`, or use the optional bundled Caddy profile if ports 80/443 are available:

```bash
docker compose --profile public up -d --build
```

**3. Open your Hub** at `https://hub.example.com`, create an account, and generate a one-time device pairing code.

See [deployment and reverse-proxy instructions](docs/DEPLOYMENT.md) for a production setup.

## Connect a computer

Run the **same versionless installer command** on Windows PowerShell or Linux, substituting your own Hub domain:

```shell
npx --yes https://hub.example.com/cli.tgz
```

The CLI verifies the agent archive, asks for its access scope and pairing code, and configures autostart. Requires **Node.js 18+**, **Python 3.10+**, and `tar`. Node.js is only needed during installation.

To inspect prerequisites without changing your computer:

```shell
npx --yes https://hub.example.com/cli.tgz --dry-run
```

- **Windows:** tray agent, autostart via Task Scheduler, optional interactive desktop control. Logs are under `%LOCALAPPDATA%\ASC-Remote-Hub\logs`.
- **Linux:** systemd user service, or a system service when installed as root. Be especially careful with execution permissions on root-owned agents.

> [!CAUTION]
> File-root restrictions apply to the Hub's built-in file tools. **Arbitrary programs run with the agent's OS privileges** when command execution is enabled. Start with read-only access and grant additional capabilities deliberately.

## Connect an MCP client

The **same endpoint** works with supported remote Streamable HTTP MCP clients:

```text
https://hub.example.com/mcp
```

Sign in with your **ASC Remote Hub account** when the OAuth consent page opens. Authorization uses PKCE; dynamic registration accepts HTTPS callbacks for web applications and loopback callbacks for native apps. A client must support the relevant MCP/OAuth flow.

### Cursor

Add an entry to `mcpServers` in your existing Cursor `mcp.json`; **keep your other servers**:

```json
{
  "mcpServers": {
    "asc-remote-hub": {
      "url": "https://hub.example.com/mcp"
    }
  }
}
```

Global configuration on Windows: `%USERPROFILE%\.cursor\mcp.json`. Follow Cursor's OAuth sign-in prompt. The JSON above is an example of the Hub entry, not an instruction to overwrite an existing configuration.

### ChatGPT

Add a custom MCP server with `https://hub.example.com/mcp`, then complete the Hub-hosted OAuth authorization.

### OpenMausBot and other agents

Choose a **remote Streamable HTTP MCP** server, enter `https://hub.example.com/mcp`, and complete OAuth sign-in. No provider-specific redirect allowlist is required. Exact UI steps vary between applications, and actual compatibility depends on the client's MCP implementation.

> [!TIP]
> In the web dashboard, **Authorized MCP applications** shows connections to your account. You can revoke an application's tokens without unregistering your devices or disconnecting other clients.

## Security

Security boundaries are enforced at multiple levels:

- **OAuth authorization-code + PKCE S256**, resource-bound bearer/refresh tokens, and explicit user consent.
- **Dynamic Client Registration** for secure HTTPS or local loopback redirect URIs. Redirects are matched exactly during authorization and token exchange.
- **Per-user isolation** of device ownership and tokens; a client cannot operate devices from another account.
- **Per-device permissions** (read, write, execution, GUI) and locally configured paths.
- **Outbound HTTPS agent communication** with distinct device credentials. Windows protects local device secrets with user-scoped DPAPI.
- **Optional traffic tracing.** When `ASC_HUB_TRACE_PAYLOADS=1` is configured, full traces are enabled for all authenticated accounts and may contain commands, file contents, and screenshots. Keep the storage private and apply retention controls.
- **Not end-to-end encrypted against the Hub administrator.** Someone with administrator privileges on the server can inspect sensitive traffic.

Only deploy to devices you own or have permission to administer. Read [SECURITY.md](SECURITY.md) before exposing remote execution capabilities.

## Versions and limitations

| Component | Current version |
| --- | --- |
| Hub server | **1.2.0** |
| Windows / Linux agent | **1.1.0** |
| Installer CLI | **1.1.0** |
| MCP transport | Streamable HTTP |
| Authentication | OAuth + PKCE + Dynamic Client Registration |

The existing 1.1.0 agents do **not** need to be re-enrolled when updating the Hub server to 1.2.0.

Current limitations: a single Hub process and in-memory task queues; background job handles may not survive agent restarts; GUI control needs an active Windows desktop; large file transfers are deliberately bounded. OAuth Client ID Metadata Documents are not yet advertised; use clients supporting dynamic registration.

See [Architecture](docs/ARCHITECTURE.md), [Deployment](docs/DEPLOYMENT.md), and [Changelog](CHANGELOG.md).

## Development

```bash
python3 -m unittest discover -s tests -q
node --check server/static/common.js
node --check server/static/auth.js
node --check server/static/dashboard.js
node --check server/installer_cli/connect.cjs
```

Contributions are welcome: see [CONTRIBUTING.md](CONTRIBUTING.md). Report security vulnerabilities privately, following [SECURITY.md](SECURITY.md).

## License

[MIT License](LICENSE). Self-hosted and open source.
