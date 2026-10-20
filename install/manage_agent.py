#!/usr/bin/env python3
"""ASC Remote Hub local lifecycle management (Windows + Linux, stdlib only).

Unpair: revoke this device token at the configured hub and remove local pairing,
       leave installed application files intact.
Uninstall: unpair, stop/disable only our own background agent, remove only
           ASC-owned install/config/log directories. Never touches other devices.
"""
from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import shutil
import signal
import subprocess
import sys
import time
from urllib.error import HTTPError, URLError

CLIENT = Path(__file__).resolve().parents[1] / "client"
sys.path.insert(0, str(CLIENT))
from bridge import check_url, decrypt_secret, request  # noqa: E402


def paths():
    home = Path.home().resolve()
    root = home / ".asc-remote-hub" / "agent"
    if os.name == "nt":
        appdata = Path(os.environ.get("APPDATA", str(home / "AppData" / "Roaming"))).resolve()
        local = Path(os.environ.get("LOCALAPPDATA", str(home / "AppData" / "Local"))).resolve()
        confdir = appdata / "ASC-Remote-Hub"
        logs = local / "ASC-Remote-Hub" / "logs"
    else:
        confdir = home / ".config" / "asc-remote-hub"
        logs = None
    return root, confdir, confdir / "agent.json", logs


def assert_safe_path(target: Path, expected: Path):
    # Compare without following the leaf, then refuse symlink/junction ancestors.
    if os.path.normcase(os.path.abspath(target)) != os.path.normcase(os.path.abspath(expected)):
        raise RuntimeError("Refusing unexpected application path: " + str(target))
    for node in (target, *list(target.parents)[:3]):
        if node.is_symlink():
            raise RuntimeError("Refusing symlink/junction in application path: " + str(node))


def run_process(args: list[str], *, check=True, timeout=35):
    result = subprocess.run(args, capture_output=True, text=True, errors="replace", timeout=timeout)
    if check and result.returncode:
        raise RuntimeError("Command failed (" + str(result.returncode) + "): " +
                           " ".join(args[:3]) + ": " + (result.stderr or result.stdout).strip()[-400:])
    return result


def revoke_pairing(config: Path, *, local_only: bool):
    if not config.is_file():
        print("На компьютере нет локальной привязки.")
        return
    if config.is_symlink():
        raise RuntimeError("Refusing symlinked agent.json")
    conf = json.loads(config.read_text(encoding="utf-8-sig"))
    hub = conf["hub_url"]
    did = conf["device_id"]
    check_url(hub)
    if not isinstance(did, str) or not did.startswith("d_"):
        raise RuntimeError("Invalid local device ID")
    if local_only:
        print("ВНИМАНИЕ: локальное отключение. Запись на старом хабе НЕ отозвана.")
        return
    secret = Path(conf["token_file"])
    if secret.parent.resolve() != config.parent.resolve() or secret.name not in ("device-key.secret", "device-key.dpapi"):
        raise RuntimeError("Unexpected device key location; refusing to modify credentials")
    token_text = secret.read_text(encoding="utf-8").strip()
    token = decrypt_secret(token_text) if os.name == "nt" else token_text
    if len(token) < 32:
        raise RuntimeError("Invalid local credential; use --local-only and revoke in the web account")
    try:
        result = request(hub, "/agent/unpair", {"confirm_device_id": did},
                         token, did, timeout=12)
    except HTTPError as exc:
        if exc.code != 401:
            raise RuntimeError("Hub returned HTTP " + str(exc.code) +
                               "; no local changes made. For migration from an unavailable hub use --local-only.") from exc
        print("Хаб отклонил прежний ключ (401); запись уже могла быть отозвана. Проверьте список устройств.")
        return
    except (URLError, TimeoutError, OSError) as exc:
        raise RuntimeError("Хаб недоступен; локальные данные сохранены. " +
                           "Для принудительного локального сброса укажите --local-only " +
                           "и отдельно отзовите устройство в старом кабинете.") from exc
    if result.get("revoked") is not True:
        raise RuntimeError("Hub did not confirm device revocation; local data preserved")
    print("Старая привязка отозвана на хабе:", hub)


def verified_linux_unit(root: Path) -> tuple[list[str], Path] | None:
    user_unit = Path.home() / ".config" / "systemd" / "user" / "asc-remote-hub-agent.service"
    system_unit = Path("/etc/systemd/system/asc-remote-hub-agent.service")
    unit = system_unit if os.geteuid() == 0 else user_unit
    if not unit.exists():
        return None
    if unit.is_symlink():
        raise RuntimeError("Systemd unit is a symlink, refusing change")
    text = unit.read_text(encoding="utf-8")
    worker = str(root / "client" / "bridge.py")
    # The unit belongs to this install only if ExecStart contains the exact worker path.
    if not any(line.startswith("ExecStart=") and worker in line.split()
               for line in text.splitlines()):
        raise RuntimeError("Existing systemd unit is not this ASC agent: " + str(unit))
    return (["systemctl"] if os.geteuid() == 0 else ["systemctl", "--user"]), unit


def stop_linux(root: Path):
    unit_spec = verified_linux_unit(root)
    if unit_spec:
        cmd, unit = unit_spec
        environment = os.environ.copy()
        if os.geteuid() != 0:
            environment.setdefault("XDG_RUNTIME_DIR", "/run/user/" + str(os.geteuid()))
            environment.setdefault("DBUS_SESSION_BUS_ADDRESS", "unix:path=" + environment["XDG_RUNTIME_DIR"] + "/bus")
        result = subprocess.run(cmd + ["disable", "--now", "asc-remote-hub-agent.service"],
                                capture_output=True, text=True, env=environment, timeout=35)
        if result.returncode != 0:
            raise RuntimeError("Cannot stop systemd agent: " + result.stderr.strip()[-400:])
        unit.unlink()
        result = subprocess.run(cmd + ["daemon-reload"], capture_output=True, text=True,
                                env=environment, timeout=20)
        if result.returncode != 0:
            raise RuntimeError("systemd daemon-reload failed: " + result.stderr.strip()[-400:])
        print("Автозапуск systemd удалён.")
    # Stop only processes running this exact installed worker in the current user's scope.
    target = str(root / "client" / "bridge.py")
    active = []
    proc = Path("/proc")
    if proc.is_dir():
        for pid_dir in proc.iterdir():
            if not pid_dir.name.isdigit() or int(pid_dir.name) == os.getpid():
                continue
            try:
                raw = (pid_dir / "cmdline").read_bytes()
                parts = [p.decode("utf-8", "replace") for p in raw.split(b"\0") if p]
                if target in parts and "run" in parts:
                    active.append(int(pid_dir.name))
            except (OSError, PermissionError):
                continue
    for pid in active:
        try:
            os.kill(pid, signal.SIGTERM)
        except ProcessLookupError:
            pass
    if active:
        time.sleep(1)
    for pid in active:
        try:
            os.kill(pid, 0)
        except ProcessLookupError:
            continue
        raise RuntimeError("ASC worker did not stop: PID " + str(pid))
    print("Фоновые процессы ASC остановлены.")


def stop_windows(root: Path):
    # Everything is executed in the current user's logon context; no elevation.
    # Only remove a task that launches OUR exact tray supervisor.
    script = r'''
$ErrorActionPreference = 'Stop'
$root = [Text.Encoding]::UTF8.GetString([Convert]::FromBase64String('ROOT_PLACEHOLDER'))
$tray = [IO.Path]::GetFullPath((Join-Path $root 'install\tray_windows.ps1'))
$worker = [IO.Path]::GetFullPath((Join-Path $root 'client\bridge.py'))
$task = Get-ScheduledTask -TaskName 'ASC Remote Hub Agent' -ErrorAction SilentlyContinue
if ($task) {
    $launch = [IO.Path]::GetFullPath((Join-Path $root 'install\launch_tray_windows.vbs'))
    $argsText = @($task.Actions | ForEach-Object { $_.Arguments }) -join ' '
    if ($argsText.IndexOf($launch,[StringComparison]::OrdinalIgnoreCase) -lt 0) {
        throw 'Scheduled task with same name does not belong to this installation'
    }
    Stop-ScheduledTask -TaskName 'ASC Remote Hub Agent' -ErrorAction SilentlyContinue
    Unregister-ScheduledTask -TaskName 'ASC Remote Hub Agent' -Confirm:$false
}
foreach ($p in @(Get-CimInstance Win32_Process -ErrorAction Stop)) {
    if (!$p.CommandLine -or $p.ProcessId -eq $PID) { continue }
    $cmd = $p.CommandLine
    if ($cmd.IndexOf($tray,[StringComparison]::OrdinalIgnoreCase) -ge 0 -or
        ($cmd.IndexOf($worker,[StringComparison]::OrdinalIgnoreCase) -ge 0 -and
         $cmd -match '\brun\b')) {
        Stop-Process -Id $p.ProcessId -Force -ErrorAction SilentlyContinue
    }
}
'''
    exe = shutil.which("powershell.exe")
    if not exe:
        raise RuntimeError("PowerShell is required for Windows agent management")
    import base64
    root_data = base64.b64encode(str(root).encode("utf-8")).decode("ascii")
    payload = script.replace("ROOT_PLACEHOLDER", root_data)
    encoded = base64.b64encode(payload.encode("utf-16le")).decode("ascii")
    run_process([exe, "-NoProfile", "-NonInteractive", "-ExecutionPolicy", "Bypass",
                 "-EncodedCommand", encoded], timeout=45)
    print("Фоновое задание и процессы ASC остановлены.")


def cleanup_pairing(config: Path, logs: Path | None):
    if config.is_file():
        if config.is_symlink():
            raise RuntimeError("Refusing symlinked config")
        conf = json.loads(config.read_text(encoding="utf-8-sig"))
        stored = Path(conf.get("token_file", ""))
        if stored.parent.resolve() != config.parent.resolve() or stored.name not in ("device-key.secret", "device-key.dpapi"):
            raise RuntimeError("Unexpected credential location, refusing cleanup")
        if stored.is_symlink():
            raise RuntimeError("Refusing symlinked device key")
        stored.unlink(missing_ok=True)
        config.unlink(missing_ok=True)
    if logs is not None:
        marker = logs / "agent.ready.json"
        if marker.is_file() and not marker.is_symlink():
            marker.unlink()
    print("Локальный ключ и привязка удалены.")


def owned_directory_remove(target: Path):
    if target.is_symlink():
        raise RuntimeError("Refusing symlinked application directory")
    if target.is_dir():
        shutil.rmtree(target)
        print("Удалено:", target)


def main():
    ap = argparse.ArgumentParser(description="ASC Remote Hub: отвязка/удаление этого компьютера")
    ap.add_argument("action", choices=("unpair", "uninstall"))
    ap.add_argument("--local-only", action="store_true",
                    help="Only local cleanup, without server revocation; revoke on old hub manually")
    ap.add_argument("--yes", action="store_true", help="Confirmed locally by caller or tray dialog")
    ap.add_argument("--dry-run", action="store_true", help="Show plan without changes")
    args = ap.parse_args()

    root, confdir, config, logs = paths()
    assert_safe_path(root, Path.home().resolve() / ".asc-remote-hub" / "agent")
    assert_safe_path(config, confdir / "agent.json")
    if not root.is_dir() and not config.is_file():
        print("ASC Remote Hub не установлен и не привязан для текущего пользователя.")
        return
    if root.is_symlink() or confdir.is_symlink():
        raise RuntimeError("Refusing symlinked install/config directories")
    if os.name != "nt":
        verified_linux_unit(root)  # preflight: don't touch an unrelated service
    print("Операция:", args.action, "\nКомпьютер:", Path.home(), "\nАгент:", root)
    print("Учетная запись ОС:", os.environ.get("USERNAME") or os.environ.get("USER", "current"))
    print("Хаб:", "локальный сброс (старый хаб может сохранить запись)" if args.local_only
          else "будет отозвана только привязка этого устройства")
    if args.dry_run:
        print("DRY_RUN_OK: изменений нет.")
        return
    if not args.yes:
        word = "ОТВЯЗАТЬ" if args.action == "unpair" else "УДАЛИТЬ"
        try:
            entry = input("Для подтверждения введите " + word + ": ").strip()
        except EOFError as exc:
            raise RuntimeError("Interactive confirmation required. No changes made.") from exc
        if entry != word:
            print("Отменено, изменений нет.")
            return
    revoke_pairing(config, local_only=args.local_only)
    if os.name == "nt":
        stop_windows(root)
    else:
        stop_linux(root)
    cleanup_pairing(config, logs)
    if args.action == "uninstall":
        owned_directory_remove(root)
        owned_directory_remove(confdir)
        if logs:
            owned_directory_remove(logs)
            local_parent = logs.parent
            if local_parent.is_dir() and not any(local_parent.iterdir()):
                local_parent.rmdir()
        if root.parent.is_dir() and not any(root.parent.iterdir()):
            root.parent.rmdir()
        print("ASC Remote Hub удалён для текущего пользователя.")
    else:
        print("Компьютер отвязан. Приложение сохранено; можно подключить другой хаб.")


if __name__ == "__main__":
    try:
        main()
    except Exception as exc:
        print("ОШИБКА: " + str(exc), file=sys.stderr)
        sys.exit(1)
