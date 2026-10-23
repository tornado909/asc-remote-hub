#!/usr/bin/env bash
# ASC Remote Hub Linux bootstrap: a small Node.js runtime is enough.
# Run latest cli.tgz directly; npm/npx and Debian's 400+ npm deps are optional.
set -euo pipefail
HUB_ORIGIN='__HUB_ORIGIN__'
MODE=install
if (( $# > 1 )); then
    printf '%s\n' 'Usage: bootstrap.sh [--check|--deps-only|--verify-cli|--cli-dry-run|--unpair|--uninstall]' >&2
    exit 2
fi
if (( $# == 1 )); then
    case "$1" in
        --check) MODE=check ;;
        --deps-only) MODE=deps ;;
        --verify-cli) MODE=verify ;;
        --cli-dry-run) MODE=cli-dry-run ;;
        --unpair) MODE=unpair ;;
        --uninstall) MODE=uninstall ;;
        *) printf 'Unknown bootstrap option: %s\n' "$1" >&2; exit 2 ;;
    esac
fi
if [[ ! "$HUB_ORIGIN" =~ ^https://([A-Za-z0-9.-]+|\[[0-9a-fA-F:]+\])(:[0-9]{1,5})?$ ]]; then
    printf '%s\n' 'Invalid HTTPS Hub origin.' >&2
    exit 2
fi
check_node() {
    command -v node >/dev/null 2>&1 || return 1
    local major
    major="$(node -p 'Number(process.versions.node.split(".")[0])' 2>/dev/null)" || return 1
    [[ "$major" =~ ^[0-9]+$ ]] && (( major >= 18 ))
}
check_python() {
    command -v python3 >/dev/null 2>&1 || return 1
    python3 -c 'import sys;sys.exit(0 if sys.version_info >= (3,10) else 1)' 2>/dev/null
}
has_downloader() {
    command -v curl >/dev/null 2>&1 || command -v wget >/dev/null 2>&1
}
has_tools() {
    command -v tar >/dev/null 2>&1 && has_downloader
}
privileged() {
    if (( EUID == 0 )); then "$@"
    elif command -v sudo >/dev/null 2>&1; then sudo "$@"
    else printf '%s\n' 'sudo is required for missing system packages.' >&2; return 1
    fi
}
node_ok=0; python_ok=0; tools_ok=0
if check_node; then node_ok=1; fi
if check_python; then python_ok=1; fi
if has_tools; then tools_ok=1; fi
printf 'Node.js >=18: %s\n' "$([[ $node_ok == 1 ]] && echo OK || echo missing)"
printf 'Python >=3.10: %s\n' "$([[ $python_ok == 1 ]] && echo OK || echo missing)"
printf 'tar and curl/wget: %s\n' "$([[ $tools_ok == 1 ]] && echo OK || echo missing)"
if [[ "$MODE" == check ]]; then
    printf '%s\n' 'No packages installed (read-only check).'
    exit 0
fi
# Check service availability before installing packages for enrollment.
if [[ "$MODE" == install ]]; then
    if ! command -v systemctl >/dev/null 2>&1; then
        printf '%s\n' 'The ASC agent requires systemd.' >&2
        exit 1
    fi
    if (( EUID == 0 )); then
        if [[ ! -d /run/systemd/system ]]; then
            printf '%s\n' 'No active systemd instance for root agent.' >&2
            exit 1
        fi
        printf '%s\n' 'WARNING: root installation creates a root-owned systemd agent.'
    elif [[ ! -d "/run/user/$(id -u)" ]]; then
        printf '%s\n' 'No active systemd user session; no packages installed.' >&2
        exit 1
    fi
fi
# Positional parameters hold only the missing distro packages.
set --
if (( !node_ok )); then set -- "$@" nodejs; fi
if (( !python_ok )); then set -- "$@" python3; fi
if ! command -v tar >/dev/null 2>&1; then set -- "$@" tar; fi
if ! has_downloader; then set -- "$@" curl ca-certificates; fi
if (( $# > 0 )); then
    printf 'Installing only missing prerequisites: %s\n' "$*"
    if command -v apt-get >/dev/null 2>&1; then
        privileged apt-get update
        privileged apt-get install -y --no-install-recommends "$@"
    elif command -v dnf >/dev/null 2>&1; then
        privileged dnf install -y "$@"
    elif command -v yum >/dev/null 2>&1; then
        privileged yum install -y "$@"
    elif command -v zypper >/dev/null 2>&1; then
        privileged zypper --non-interactive install "$@"
    else
        printf '%s\n' 'Unsupported package manager (apt/dnf/yum/zypper).' >&2
        exit 1
    fi
    # Ignore outdated nvm shims when a suitable system Node exists.
    if ! check_node && [[ -x /usr/bin/node ]]; then export PATH="/usr/bin:$PATH"; fi
fi
if ! check_node; then
    printf '%s\n' 'Node.js >=18 unavailable. Your distro may ship an older version.' >&2
    exit 1
fi
if ! check_python || ! has_tools; then
    printf '%s\n' 'Python >=3.10, tar or curl/wget is still missing.' >&2
    exit 1
fi
printf '%s\n' 'Dependencies ready. npm/npx are not needed for bootstrap or agent.'
if [[ "$MODE" == deps ]]; then
    printf 'Run the same %s/bootstrap.sh without flags to connect.\n' "$HUB_ORIGIN"
    exit 0
fi
tmp="$(mktemp -d -t asc-hub-cli-XXXXXXXX)"
cleanup() { rm -rf -- "$tmp"; }
trap cleanup EXIT
archive="$tmp/cli.tgz"
if command -v curl >/dev/null 2>&1; then
    curl --fail --silent --show-error --location --retry 2 --max-time 120 \
        "$HUB_ORIGIN/cli.tgz" --output "$archive"
else
    wget --quiet --timeout=120 -O "$archive" "$HUB_ORIGIN/cli.tgz"
fi
size="$(wc -c < "$archive")"
if (( size < 1000 || size > 25000000 )); then
    printf '%s\n' 'Invalid CLI package size.' >&2
    exit 1
fi
listing="$(tar -tzf "$archive")"
member_count=0
while IFS= read -r item; do
    (( member_count += 1 ))
    case "$item" in
        package/README.md|package/package.json|package/bin/connect.cjs|package/assets/asc-remote-hub-agent-*.tar.gz) ;;
        *) printf 'Unexpected CLI archive member: %s\n' "$item" >&2; exit 1 ;;
    esac
    if [[ "$item" == *".."* || "$item" == *'\'* ]]; then
        printf '%s\n' 'Unsafe CLI archive member.' >&2
        exit 1
    fi
done <<< "$listing"
if (( member_count != 4 )); then
    printf 'Unexpected CLI archive: %s members\n' "$member_count" >&2
    exit 1
fi
tar -xzf "$archive" -C "$tmp" --no-same-owner --no-same-permissions
cli="$tmp/package/bin/connect.cjs"
if [[ ! -s "$cli" ]]; then
    printf '%s\n' 'CLI executable not found in archive.' >&2
    exit 1
fi
if [[ "$MODE" == verify ]]; then
    node "$cli" --version
elif [[ "$MODE" == cli-dry-run ]]; then
    node "$cli" --dry-run
elif [[ "$MODE" == unpair ]]; then
    node "$cli" unpair
elif [[ "$MODE" == uninstall ]]; then
    node "$cli" uninstall
else
    # Keep the terminal for entering the one-time pairing code.
    node "$cli"
fi
