#!/usr/bin/env python3
"""Local-only ASC Game Agent policy editor for the Windows tray.

Does not touch pairing credentials, does not contact the Hub and does not
launch native Game Agent components. Existing agent configuration is retained.
"""
from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import tempfile

FLAGS = ("enabled", "capture", "gpu_test", "open_vocab")


def default_config() -> Path:
    if os.name != "nt":
        raise RuntimeError("Game Agent settings are only available on Windows")
    return Path(os.environ["APPDATA"]) / "ASC-Remote-Hub" / "agent.json"


def read_config(path: Path) -> dict:
    if path.is_symlink() or not path.is_file() or path.parent.is_symlink():
        raise RuntimeError("Expected an installed, regular ASC Remote Hub configuration")
    with path.open("r", encoding="utf-8-sig") as handle:
        data = json.load(handle)
    if not isinstance(data, dict) or not isinstance(data.get("permissions"), dict):
        raise ValueError("Invalid ASC Remote Hub configuration")
    if not isinstance(data.get("hub_url"), str) or not data["hub_url"].startswith("https://"):
        raise ValueError("Expected existing HTTPS Hub enrollment")
    if not isinstance(data.get("device_id"), str) or not data["device_id"]:
        raise ValueError("Missing existing device enrollment")
    return data


def policy_of(config: dict) -> dict[str, bool]:
    game = config.get("game_agent", {})
    if not isinstance(game, dict):
        raise ValueError("Invalid game_agent settings")
    if any(key not in FLAGS or type(value) is not bool for key, value in game.items()):
        raise ValueError("Invalid game_agent flags")
    return {k: bool(game.get(k, False)) for k in FLAGS}


def effective_policy(config: dict) -> dict[str, bool]:
    perms = config["permissions"]
    # Discovery and GPU capabilities must be explicitly permitted by
    # the local user *and* by existing exec+GUI permissions.
    allowed = perms.get("exec") is True and perms.get("gui") is True
    game = policy_of(config)
    enabled = allowed and game["enabled"]
    return {"enabled": enabled,
            "capture": enabled and game["capture"],
            "gpu_test": enabled and game["gpu_test"],
            "open_vocab": enabled and game["capture"] and game["open_vocab"]}


def update_policy(config: dict, flag: str, value: bool) -> dict:
    if flag not in FLAGS or type(value) is not bool:
        raise ValueError("Unknown Game Agent flag/value")
    # Fail without touching the original file if permissions are insufficient.
    effective = effective_policy(config)
    game = policy_of(config)
    if value and (config["permissions"].get("exec") is not True or
                  config["permissions"].get("gui") is not True):
        raise RuntimeError("Game Agent requires local GUI and execution permissions")
    if value and flag != "enabled" and not effective["enabled"]:
        raise RuntimeError("Enable ASC Game Agent before changing GPU/capture policy")
    if value and flag == "open_vocab" and not effective["capture"]:
        raise RuntimeError("Enable locally consented capture before Universal Vision v22")
    game[flag] = value
    if flag == "capture" and not value:
        game["open_vocab"] = False
    if flag == "enabled" and not value:
        game["capture"] = False
        game["gpu_test"] = False
        game["open_vocab"] = False
    config = dict(config)
    config["game_agent"] = game
    return config


def write_config(path: Path, config: dict) -> None:
    if path.is_symlink() or path.parent.is_symlink():
        raise RuntimeError("Refusing symlink configuration")
    raw = json.dumps(config, ensure_ascii=False, indent=2) + "\n"
    fd, temporary = tempfile.mkstemp(prefix=".game-agent-",suffix=".tmp",dir=path.parent)
    try:
        if os.name != "nt":
            os.fchmod(fd, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as out:
            out.write(raw)
            out.flush()
            os.fsync(out.fileno())
        # Atomic replace: the existing pairing and DPAPI token path remain.
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(description="Local ASC Game Agent settings")
    parser.add_argument("action", choices=["status", "set"])
    parser.add_argument("flag", choices=FLAGS, nargs="?")
    parser.add_argument("value", choices=["on", "off"], nargs="?")
    args = parser.parse_args(argv)
    config_path = default_config()
    config = read_config(config_path)
    if args.action == "set":
        if args.flag is None or args.value is None:
            parser.error("set requires one flag and on/off")
        updated = update_policy(config,args.flag,args.value=="on")
        write_config(config_path,updated)
        config=updated
    elif args.flag is not None or args.value is not None:
        parser.error("status does not take flag/value")
    print(json.dumps({"ok": True, "game_agent": effective_policy(config),
                      "requires_restart": args.action == "set"}))


if __name__ == "__main__":
    main()
