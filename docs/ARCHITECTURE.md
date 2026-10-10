# Architecture

ChatGPT connects by OAuth + HTTPS to the public MCP endpoint. The hub is one Python HTTP process with SQLite in WAL mode, a per-device in-memory queue, error history and per-user tokens. Windows/Linux agents initiate outbound-only HTTPS polls and run tools in their OS user context. Long command execution includes a separate heartbeat to prevent false offline indicators.

Core security boundaries: OAuth user identity -> owner check for each device -> unique device credential -> agent local permissions. Agent directory roots restrict built-in file tools, not arbitrary child processes started with execution permissions.

The hub generates two archives on startup: an npm-compatible CLI tarball bound to ASC_HUB_URL and a vetted Python agent archive. Only explicitly allowlisted files may enter these archives; runtime configuration, DB data and user logs are never included.

Known limits: one server process; job queue not durable across service restarts; background job handles not guaranteed after agent restarts; bounded file operations; optional desktop control depends on the interactive session; per-device tasks may execute sequentially. This is designed for personal or small-group use, not distributed HPC scheduling.

Future options: persistent job queue with ACK and idempotency, resumable chunked transfer, stronger sandboxing, per-capability OAuth scopes and an agent version/upgrade manager.
