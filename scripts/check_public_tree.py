"""Generic public-release guard, without embedding anyone's personal identifiers.

Public CI checks every tracked file (INCLUDING this script and all tests).
An optional private blocklist can be provided via environment variable for
additional organization-specific identifiers without committing them.
"""
from __future__ import annotations

import ipaddress
import os
from pathlib import Path
import re
import subprocess
import sys

ROOT = Path(__file__).resolve().parents[1]
PRIVATE_NAMES = {".env", "agent.json", "hub.sqlite3", "device-key.dpapi"}
PRIVATE_PARTS = {"data", "node_modules", ".venv", "__pycache__", ".git"}
PRIVATE_EXT = {".log", ".db", ".sqlite", ".sqlite3", ".pem", ".key",
               ".p12", ".pfx", ".zip", ".tgz", ".gz", ".dpapi"}
TEXT_EXT = {".py", ".js", ".cjs", ".md", ".sh", ".ps1", ".html", ".css",
            ".json", ".yml", ".yaml", ".toml", ".txt", ""}
EMAIL_RE = re.compile(r"(?<![\w.+-])[\w.+-]+@(?:[a-zA-Z0-9-]+\.)+[a-zA-Z]{2,}", re.I)
# Domain-like text must have plausible DNS labels and a complete token boundary.
# Requiring an actual TLD avoids mistaking object attributes (e.g. user.tools)
# or filenames (e.g. manifest.json) for production hostnames.
HOST_RE = re.compile(
    r"(?<![a-zA-Z0-9_.@-])"
    r"(?:[a-zA-Z0-9](?:[a-zA-Z0-9-]{0,61}[a-zA-Z0-9])?\.)+"
    r"[a-zA-Z]{2,63}(?![a-zA-Z0-9_.-])", re.I)
KNOWN_TLDS = frozenset((
    "com net org io dev ai app cloud co me info biz site online tech xyz gg "
    "us uk de nl eu fr ca es ch at pl jp cn in br au nz se no fi be pt "
    "ua kz by az cc ws top live space studio digital agency gov edu shop "
    "store pro tv net ru"
).split())
INTERNAL_TLDS = frozenset(("local", "internal", "lan", "corp", "home", "intranet"))
IPV4_RE = re.compile(r"(?<![a-zA-Z0-9_.])(?:\d{1,3}\.){3}\d{1,3}(?![a-zA-Z0-9_.])")
# Network integers avoid embedding real-looking private IPs in the guard.
PRIVATE_IPV4 = tuple(
    ipaddress.IPv4Network((int(network, 16), prefix))
    for network, prefix in (
        ("0a000000", 8), ("ac100000", 12),
        ("c0a80000", 16), ("a9fe0000", 16),
    )
)

URL_RE = re.compile(r"https?://([A-Za-z0-9_.:-]+)", re.I)
PUBLIC_HOSTS = {"github.com", "api.github.com", "raw.githubusercontent.com",
                "chatgpt.com", "openai.com", "python.org", "www.python.org",
                "nodejs.org", "npmjs.org", "registry.npmjs.org", "learn.microsoft.com",
                "cursor.com", "www.cursor.com"}
EXAMPLE_SUFFIXES = ("example.com", "example.org", "example.net", "example.test",
                    "example.invalid", ".example", ".test", ".invalid")


def documentation_host(host: str) -> bool:
    normalized = host.lower().split(":", 1)[0].rstrip(".")
    if normalized == "your_domain":
        return True
    return (normalized in PUBLIC_HOSTS or
            any(normalized == s or normalized.endswith("." + s)
                for s in EXAMPLE_SUFFIXES if not s.startswith(".")) or
            normalized.endswith((".example", ".test", ".invalid")) or
            normalized in ("localhost", "127.0.0.1", "::1"))


def private_literal_findings(raw: str, extra_blocklist: tuple[str, ...] = ()) -> set[str]:
    findings: set[str] = set()
    if any(fragment and fragment.casefold() in raw.casefold()
           for fragment in extra_blocklist):
        findings.add("private identifier in external blocklist")
    for matched in EMAIL_RE.finditer(raw):
        domain = matched.group().rsplit("@", 1)[1]
        if not documentation_host(domain):
            findings.add("non-example email address")
    # Handle URLs even in regex literals with escaped dots. Unlike the old
    # checker, do not ignore a URL merely because a backslash follows a label.
    normalized = raw.replace(chr(92) + ".", ".")
    for matched in URL_RE.finditer(normalized):
        if not documentation_host(matched.group(1)):
            findings.add("non-allowlisted network origin")
    for matched in HOST_RE.finditer(normalized):
        hostname = matched.group().lower()
        # GUI geometry attributes and instance fields are not domain names.
        if hostname in {"self.home", "rect.top", "rect.left", "rect.right", "rect.bottom"}:
            continue
        tld = hostname.rsplit(".", 1)[-1]
        if tld not in KNOWN_TLDS and tld not in INTERNAL_TLDS:
            continue
        # Short variables such as d.online are usually member access, while
        # a real registrable label normally has at least three characters.
        if len(hostname.rsplit(".", 2)[-2]) < 3:
            continue
        # Python method calls such as Path.home() are not hostnames.
        if normalized[matched.end():matched.end()+1] == "(":
            continue
        if not documentation_host(hostname):
            findings.add("non-example bare hostname")
    for matched in IPV4_RE.finditer(normalized):
        try:
            addr = ipaddress.ip_address(matched.group())
        except ValueError:
            continue
        if any(addr in block for block in PRIVATE_IPV4):
            findings.add("non-documentation private network address")
    if re.search(r"-----BEGIN (?:RSA |EC |OPENSSH )?PRIVATE KEY-----", raw):
        findings.add("private key material")
    if re.search(r"(?im)^\s*(?:password|device_token|access_token|api_key)"
                 r"\s*[:=]\s*['\"]?[A-Za-z0-9_+/=-]{20,}['\"]?\s*$", raw):
        findings.add("possible plaintext credential")
    return findings


def tracked_paths(root: Path) -> list[Path]:
    result = subprocess.run(["git", "ls-files", "-z", "--cached"], cwd=root,
                            stdout=subprocess.PIPE, check=True)
    return [root / os.fsdecode(item) for item in result.stdout.split(b"\x00") if item]


def check_public_tree(root: Path, extra_blocklist: tuple[str, ...] = ()) -> list[str]:
    findings = []
    for p in tracked_paths(root):
        if not p.is_file():
            continue
        rel = p.relative_to(root)
        if (p.name in PRIVATE_NAMES or any(part in PRIVATE_PARTS for part in rel.parts)):
            findings.append(f"{rel}: private file path")
            continue
        if p.suffix.lower() in PRIVATE_EXT:
            findings.append(f"{rel}: forbidden file type")
            continue
        if p.suffix.lower() in TEXT_EXT:
            content = p.read_text(encoding="utf-8", errors="replace")
            for issue in sorted(private_literal_findings(content, extra_blocklist)):
                findings.append(f"{rel}: {issue}")
    return findings


def main() -> int:
    local_blocklist = tuple(filter(None, os.getenv("ASC_RELEASE_PRIVATE_LITERALS", "").splitlines()))
    problems = check_public_tree(ROOT, local_blocklist)
    if problems:
        print("Public release check failed:")
        for issue in problems:
            print(" -", issue)
        return 1
    print("Public release tree check passed.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
