#!/usr/bin/env bash
# ASC Remote Hub: per-user agent, or root-owned systemd agent when run as root.
set -euo pipefail
if [[ $# -lt 1 ]];then
    echo 'Usage: bash install/install_linux.sh https://hub.example.com [device_name] [roots] [read-only|full]' >&2
    exit 2
fi
BASE="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
URL="$1"
DEVICE="${2:-$(hostname)}"
ROOT="${3:-$HOME}"
MODE="${4:-read-only}"
CONFIG="$HOME/.config/asc-remote-hub/agent.json"
IFS=";" read -r -a ROOTS <<< "$ROOT"
ARGS=(enroll --hub "$URL" --name "$DEVICE" --roots "${ROOTS[@]}" --config "$CONFIG")
if [[ "$MODE" == full ]];then
    ARGS+=(--write --exec)
elif [[ "$MODE" != "read-only" ]];then
    echo 'Permission mode must be read-only or full.' >&2
    exit 2
fi
PYTHON="$(command -v python3)"
[[ -n "$PYTHON" ]] || { echo 'Python 3 not found' >&2; exit 1; }

# Validate service manager BEFORE consuming the one-time pairing code.
IS_ROOT=0
if [[ "$(id -u)" == 0 ]];then
    IS_ROOT=1
    if [[ ! -d /run/systemd/system ]] || ! command -v systemctl >/dev/null;then
        echo 'Root installation requires an active systemd instance.' >&2
        exit 1
    fi
    echo 'WARNING: Installing a root-owned systemd agent.'
    if [[ "$MODE" == full ]];then
        echo 'WARNING: Remote commands will execute with root privileges.'
    fi
else
    if [[ ! -d "/run/user/$(id -u)" ]] || ! command -v systemctl >/dev/null;then
        echo 'User installation requires a running systemd user session.' >&2
        exit 1
    fi
fi
if [[ "${ASC_HUB_SKIP_ENROLL:-}" != "1" ]];then
    "$PYTHON" "$BASE/client/bridge.py" "${ARGS[@]}"
else
    [[ -f "$CONFIG" ]] || { echo "No existing device config to restore" >&2; exit 1; }
    echo "Existing enrollment preserved; restoring background service."
fi
chmod 700 "$(dirname "$CONFIG")"
[[ -f "$CONFIG" ]] && chmod 600 "$CONFIG"
[[ -f "$(dirname "$CONFIG")/device-key.secret" ]] && chmod 600 "$(dirname "$CONFIG")/device-key.secret"

if (( IS_ROOT ));then
    SERVICE=/etc/systemd/system/asc-remote-hub-agent.service
    printf '[Unit]\nDescription=ASC Remote Hub outbound root agent\nAfter=network-online.target\nWants=network-online.target\n\n[Service]\nType=simple\nUser=root\nWorkingDirectory=%s\nExecStart=%s %s run --config %s\nRestart=always\nRestartSec=4\nUMask=0077\nNoNewPrivileges=true\n\n[Install]\nWantedBy=multi-user.target\n' \
        "$BASE" "$PYTHON" "$BASE/client/bridge.py" "$CONFIG" > "$SERVICE"
    chmod 644 "$SERVICE"
    systemctl daemon-reload
    systemctl enable --now asc-remote-hub-agent.service
    echo 'Root agent started: systemctl status asc-remote-hub-agent.service'
else
    mkdir -p "$HOME/.config/systemd/user"
    SERVICE="$HOME/.config/systemd/user/asc-remote-hub-agent.service"
    printf '[Unit]\nDescription=ASC Remote Hub outbound agent\nAfter=network-online.target\nWants=network-online.target\n\n[Service]\nType=simple\nExecStart=%s %s run --config %s\nRestart=always\nRestartSec=4\nUMask=0077\n\n[Install]\nWantedBy=default.target\n' \
        "$PYTHON" "$BASE/client/bridge.py" "$CONFIG" > "$SERVICE"
    export XDG_RUNTIME_DIR="${XDG_RUNTIME_DIR:-/run/user/$(id -u)}"
    export DBUS_SESSION_BUS_ADDRESS="${DBUS_SESSION_BUS_ADDRESS:-unix:path=$XDG_RUNTIME_DIR/bus}"
    systemctl --user daemon-reload
    systemctl --user enable --now asc-remote-hub-agent.service
    echo 'Agent started: systemctl --user status asc-remote-hub-agent.service'
fi
