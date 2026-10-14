# Deploy ASC Remote Hub on any domain

## Prerequisites

Linux with Docker Engine, Docker Compose v2, DNS name you own, HTTPS certificate, adequate persistent disk and permission to administer the network.

## Configure

Clone https://github.com/tornado909/asc-remote-hub and copy .env.example to .env. Set the following (replace hub.example.com):

    ASC_HUB_URL=https://hub.example.com
    ASC_HUB_DOMAIN=hub.example.com
    ASC_BIND_IP=127.0.0.1
    ASC_BIND_PORT=18765
    PUID=1000
    PGID=1000
    ASC_HUB_TRACE_PAYLOADS=0

Create a private data folder (chmod 700, owned by PUID:PGID), then run:

    docker compose up -d --build hub

Configure an external HTTPS reverse proxy forwarding to the internal HTTP endpoint port 18765, preserving Host and Authorization. Only expose the HTTP endpoint to that proxy. If your host has unused ports 80/443, the bundled Caddy profile is available:

    docker compose --profile public up -d --build

The canonical host is ASC_HUB_URL. The server uses it for OAuth, links and generating versioned installers. No hard-coded developer domain is needed.

## Create administrator

From an interactive terminal:

    docker compose exec -it hub python3 server/hub.py create-admin

Other users may register freely through the site. The application admin UI shows aggregate stats only, not other users' devices.

## Connect devices and MCP clients

From the site, copy the npx installation command generated for its domain. Required on the client: Node.js 18+, Python 3.10+ and tar. A one-time pairing code is provided after login; the installer asks for the code and chosen permissions. On Windows, a user session is needed for GUI operation.

Add the remote Streamable HTTP MCP endpoint to ChatGPT, Cursor, OpenMausBot, or any compatible client:

    https://hub.example.com/mcp

Choose OAuth and log into your own Hub account. The Hub uses authorization code + PKCE S256 and supports dynamic client registration with HTTPS callbacks or native-app loopback HTTP callbacks (with an explicit port). The client must support these MCP/OAuth mechanisms. A per-user application list in the dashboard lets you revoke individual clients without removing devices. The Hub server may run 1.2.0 while agents and the installer remain 1.1.0; re-enrollment is not required. Client-specific plan limits are independent of the Hub.

## Updates

Back up .env and data/hub.sqlite3 securely before upgrading. Pull a reviewed release, run the Python unit tests, then docker compose up -d --build hub. Check /healthz, both device connections, OAuth, static site and the versioned npx --dry-run command.

Troubleshooting: invalid roots produce AccessDenied; an Unknown job_id can occur after an agent restart; network loss may cause temporary timeouts. See the error history in the web panel.
