#!/usr/bin/env bash
# ASC Remote Hub Linux bootstrap: only official distro packages.
# No third-party curl|bash package-manager repositories are added.
# Enrollment remains inside the existing checked CLI.
set -euo pipefail

HUB_ORIGIN='__HUB_ORIGIN__'
MODE=install
if (( $# > 1 )); then
    printf '%s\n' 'Usage: bootstrap.sh [--deps-only|--check]' >&2
    exit 2
fi
if (( $# == 1 )); then
    case "$1" in
        --deps-only) MODE=deps ;;
        --check) MODE=check ;;
        *) printf '%s\n' 'Unknown option; use --deps-only or --check.' >&2; exit 2 ;;
    esac
fi
if [[ ! "$HUB_ORIGIN" =~ ^https://([A-Za-z0-9.-]+|\[[0-9a-fA-F:]+\])(:[0-9]{1,5})?$ ]]; then
    printf '%s\n' 'Invalid HTTPS Hub origin.' >&2
    exit 2
fi

check_node() {
    command -v node >/dev/null 2>&1 || return 1
    command -v npx >/dev/null 2>&1 || return 1
    local major
    major="$(node -p 'Number(process.versions.node.split(".")[0])' 2>/dev/null)" || return 1
    [[ "$major" =~ ^[0-9]+$ ]] && (( major >= 18 ))
}
check_python() {
    command -v python3 >/dev/null 2>&1 || return 1
    python3 -c 'import sys;sys.exit(0 if sys.version_info >= (3,10) else 1)' 2>/dev/null
}
check_venv() {
    check_python || return 1
    # Debian may ship python3-venv as a separately installed package.
    local tmp
    tmp="$(mktemp -d -t asc-hub-venv-XXXXXXXX)" || return 1
    if python3 -m venv "$tmp/env" >/dev/null 2>&1; then
        rm -rf -- "$tmp"
        return 0
    fi
    rm -rf -- "$tmp"
    return 1
}
has_tools() {
    command -v tar >/dev/null 2>&1 &&
    (command -v curl >/dev/null 2>&1 || command -v wget >/dev/null 2>&1)
}

node_ok=0; python_ok=0; venv_ok=0; tools_ok=0
if check_node; then node_ok=1; fi
if check_python; then python_ok=1; fi
if check_venv; then venv_ok=1; fi
if has_tools; then tools_ok=1; fi
printf 'Node.js >=18 and npx: %s\n' "$([[ $node_ok == 1 ]] && echo OK || echo missing)"
printf 'Python >=3.10: %s\n' "$([[ $python_ok == 1 ]] && echo OK || echo missing)"
printf 'Python venv: %s\n' "$([[ $venv_ok == 1 ]] && echo OK || echo missing)"
printf 'tar and HTTP downloader: %s\n' "$([[ $tools_ok == 1 ]] && echo OK || echo missing)"

if [[ "$MODE" == check ]]; then
    printf '%s\n' 'Read-only dependency check completed; no packages were installed.'
    exit 0
fi

# Fail before installing packages if the host cannot run the agent.
if [[ "$MODE" == install ]]; then
    if ! command -v systemctl >/dev/null 2>&1; then
        printf '%s\n' 'ASC agent requires systemd (systemctl missing).' >&2
        exit 1
    fi
    if (( EUID == 0 )); then
        if [[ ! -d /run/systemd/system ]]; then
            printf '%s\n' 'Root ASC agent requires active systemd; no packages installed.' >&2
            exit 1
        fi
        printf '%s\n' 'WARNING: Running as root will register a root-owned systemd agent.'
    else
        if [[ ! -d "/run/user/$(id -u)" ]]; then
            printf '%s\n' 'No active systemd user session; no packages installed.' >&2
            exit 1
        fi
    fi
fi

privileged() {
    if (( EUID == 0 )); then
        "$@"
    elif command -v sudo >/dev/null 2>&1; then
        sudo "$@"
    else
        printf '%s\n' 'sudo is required to install distro packages.' >&2
        return 1
    fi
}

if (( !node_ok || !python_ok || !venv_ok || !tools_ok )); then
    if command -v apt-get >/dev/null 2>&1; then
        printf '%s\n' 'Installing Node.js, npm, Python, venv, tar and curl via apt...'
        privileged apt-get update
        privileged apt-get install -y --no-install-recommends \
            nodejs npm python3 python3-venv tar curl ca-certificates
    elif command -v dnf >/dev/null 2>&1; then
        printf '%s\n' 'Installing Node.js, npm, Python and tools via dnf...'
        privileged dnf install -y nodejs npm python3 python3-pip tar curl ca-certificates
    elif command -v yum >/dev/null 2>&1; then
        printf '%s\n' 'Installing Node.js, npm, Python and tools via yum...'
        privileged yum install -y nodejs npm python3 python3-pip tar curl ca-certificates
    elif command -v zypper >/dev/null 2>&1; then
        printf '%s\n' 'Installing Node.js, npm, Python and tools via zypper...'
        privileged zypper --non-interactive install nodejs npm python3 python3-pip tar curl ca-certificates
    else
        printf '%s\n' 'Unsupported package manager. Supported: apt, dnf, yum, zypper.' >&2
        exit 1
    fi
    # Some old nvm shims precede a newly installed distro Node in PATH.
    if ! check_node && [[ -x /usr/bin/node ]] && [[ -x /usr/bin/npx ]]; then
        export PATH="/usr/bin:$PATH"
    fi
fi

if ! check_node; then
    printf '%s\n' 'Modern Node.js >=18 and npx not found after package installation.' >&2
    printf '%s\n' 'This distro may ship an outdated Node.js. Update the OS or use a supported package.' >&2
    exit 1
fi
if ! check_python || ! check_venv || ! has_tools; then
    printf '%s\n' 'Python >=3.10, venv, tar or HTTP downloader still missing.' >&2
    printf '%s\n' 'This Linux release may be too old. Fix distro packages before retrying.' >&2
    exit 1
fi

printf '%s\n' 'Dependencies verified: Node.js, npm/npx, Python and venv.'
if [[ "$MODE" == deps ]]; then
    printf 'Next: npx --yes %s/cli.tgz\n' "$HUB_ORIGIN"
    exit 0
fi

printf 'Starting ASC Remote Hub installer from %s\n' "$HUB_ORIGIN"
# Run from an actual terminal so the one-time pairing code can be entered.
exec npx --yes "$HUB_ORIGIN/cli.tgz"
