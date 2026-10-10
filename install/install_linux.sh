#!/usr/bin/env bash
# Run as the target unprivileged desktop/server user, NOT as root.
set -euo pipefail
if [[ "$(id -u)" == 0 ]];then echo 'Run without sudo' >&2;exit 1;fi
if [[ $# -lt 1 ]];then echo 'Usage: bash install/install_linux.sh https://hub.example.com [device_name] [root] [read-only|full]' >&2;exit 2;fi
BASE="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
URL="$1"
DEVICE="${2:-$(hostname)}"
ROOT="${3:-$HOME}"
MODE="${4:-read-only}"
CONFIG="$HOME/.config/asc-remote-hub/agent.json"
IFS=";" read -r -a ROOTS <<< "$ROOT"
ARGS=(enroll --hub "$URL" --name "$DEVICE" --roots "${ROOTS[@]}" --config "$CONFIG")
if [[ "$MODE" == full ]];then ARGS+=(--write --exec);fi
python3 "$BASE/client/bridge.py" "${ARGS[@]}"
mkdir -p "$HOME/.config/systemd/user"
SERVICE="$HOME/.config/systemd/user/asc-remote-hub-agent.service"
printf '[Unit]\nDescription=ASC Remote Hub outbound agent\nAfter=network-online.target\nWants=network-online.target\n\n[Service]\nType=simple\nExecStart=%s %s run --config %s\nRestart=always\nRestartSec=4\n\n[Install]\nWantedBy=default.target\n' "$(command -v python3)" "$BASE/client/bridge.py" "$CONFIG" > "$SERVICE"
if command -v systemctl >/dev/null; then
    export XDG_RUNTIME_DIR="${XDG_RUNTIME_DIR:-/run/user/$(id -u)}"
    export DBUS_SESSION_BUS_ADDRESS="${DBUS_SESSION_BUS_ADDRESS:-unix:path=$XDG_RUNTIME_DIR/bus}"
    systemctl --user daemon-reload
    systemctl --user enable --now asc-remote-hub-agent.service
    echo 'Agent started: systemctl --user status asc-remote-hub-agent.service'
else
    echo "Start manually: python3 '$BASE/client/bridge.py' run --config '$CONFIG'"
fi
