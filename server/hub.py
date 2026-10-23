#!/usr/bin/env python3
"""ASC Remote Hub: self-hosted multi-tenant HTTP MCP / OAuth / outbound agent relay.

No OpenAI Platform key; no paid tunnel; no OpenAI Responses API. Standard library
server, SQLite in WAL mode, a single serving process. HTTPS termination is a
REQUIRED reverse-proxy responsibility outside test mode.
"""
from __future__ import annotations
import argparse
import base64
from collections import defaultdict, deque
from contextlib import contextmanager
import hashlib
import hmac
import html
from http.cookies import SimpleCookie
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import os
from pathlib import Path
import re
import secrets
import sqlite3
import threading
import time
from urllib.parse import parse_qs, quote, urlencode, urlparse
import ipaddress
import uuid
import getpass
import ipaddress
from traffic_trace import TrafficTrace
from distribution import build_distributions, CLI, AGENT

VERSION = "1.2.6"
PROTOCOLS = {"2025-11-25", "2025-06-18", "2025-03-26", "2024-11-05"}
MAX_REQUEST = 9 * 1024 * 1024
MAX_JSON = 9 * 1024 * 1024
MAX_FORM = 16_384
# OAuth/MCP are token-based, unlike the cookie-authenticated account API.
# Browser-based public OAuth clients need cross-origin DCR, token and MCP calls.
# No CORS is granted to /oauth/authorize, /oauth/approve, /api/* or /agent/*.
CORS_PUBLIC_PATHS = frozenset({
    "/mcp", "/oauth/register", "/oauth/token",
    "/.well-known/oauth-protected-resource",
    "/.well-known/oauth-protected-resource/mcp",
    "/.well-known/oauth-authorization-server",
    "/.well-known/openid-configuration",
})
CORS_POST_PATHS = frozenset({"/mcp", "/oauth/register", "/oauth/token"})
CORS_ALLOWED_HEADERS = frozenset({
    "authorization", "content-type", "mcp-protocol-version",
    "mcp-session-id", "accept", "last-event-id",
})
def valid_oauth_redirect(uri: str) -> bool:
    """Secure exact-match redirect registration for any OAuth public client.
    Web apps use HTTPS. Native apps may use HTTP ONLY on loopback with an
    explicit nonzero port. Never use string-prefix matching for hostnames.
    """
    if (not isinstance(uri,str) or not 1<=len(uri)<=2048 or
            any(ord(char)<33 or ord(char)==127 for char in uri) or
            "\\" in uri):
        return False
    try:
        u=urlparse(uri)
        host=u.hostname
        port=u.port  # validates malformed/out-of-range port
        if (not host or not u.netloc or u.fragment or
                u.username is not None or u.password is not None or
                (u.path and (not u.path.startswith("/") or u.path.startswith("//")))):
            return False
        # Do not accept percent-encoded hostnames, ambiguous authorities or
        # unbracketed IPv6 literals. Full redirect URI is stored and matched.
        if "%" in u.netloc or "@" in u.netloc:
            return False
        if u.scheme=="https":
            if not re.fullmatch(r"[A-Za-z0-9.\-]+",host) and ":" not in host:
                return False
            return True
        if u.scheme=="http":
            if port is None or port<1:
                return False
            if host.lower()=="localhost":
                return True
            try:
                return ipaddress.ip_address(host).is_loopback
            except ValueError:
                return False
        return False
    except (ValueError,TypeError):
        return False

EMAIL_RE = re.compile(r"^[^\s@]{1,70}@[^\s@]{1,180}\.[^\s@]{2,40}$")
PKCE_RE = re.compile(r"^[A-Za-z0-9_-]{43}$")


def digest(secret: str) -> str:
    return hashlib.sha256(secret.encode("utf-8")).hexdigest()


def hash_password(password: str, salt: bytes | None = None) -> str:
    if not isinstance(password, str) or len(password) < 12 or len(password) > 512:
        raise ValueError("Password must contain 12-512 characters")
    salt = salt or secrets.token_bytes(16)
    iterations = 500_000
    raw = hashlib.pbkdf2_hmac("sha256", password.encode(), salt, iterations)
    return f"pbkdf2-sha256${iterations}${salt.hex()}${raw.hex()}"


def verify_password(password: str, saved: str) -> bool:
    try:
        algorithm, iterations, salt, hexdigest = saved.split("$")
        if algorithm != "pbkdf2-sha256" or not 100_000 <= int(iterations) <= 1_000_000:
            return False
        guess = hashlib.pbkdf2_hmac("sha256", password.encode(), bytes.fromhex(salt), int(iterations))
        return hmac.compare_digest(guess, bytes.fromhex(hexdigest))
    except (ValueError, AttributeError):
        return False


def pkce(verifier: str) -> str:
    return base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).decode().rstrip("=")


def json_text(data: dict, error: bool = False) -> dict:
    return {"content": [{"type": "text", "text": json.dumps(data, ensure_ascii=False, separators=(",", ":"))}],
            "isError": bool(error)}


class Failure(Exception):
    def __init__(self, status: int, message: str) -> None:
        self.status = status
        self.message = message
        super().__init__(message)


class Hub:
    def __init__(self, *, base_url: str, data_dir: str, test_mode: bool = False) -> None:
        parsed = urlparse(base_url)
        if (parsed.scheme != "https" and not test_mode) or parsed.scheme not in ("https", "http") or not parsed.netloc or parsed.path not in ("", "/") or parsed.query or parsed.fragment or parsed.username:
            raise ValueError("ASC_HUB_URL must be an HTTPS origin without a path")
        self.base = base_url.rstrip("/")
        self.resource = self.base + "/mcp"
        self.test_mode = test_mode
        # Trust forwarded client IPs only if the immediate TCP peer is a
        # configured reverse proxy. Untrusted headers cannot bypass throttles.
        proxy_setting=os.environ.get("ASC_HUB_TRUSTED_PROXIES","").strip()
        self.trusted_proxies=[]
        if proxy_setting:
            self.trusted_proxies=[ipaddress.ip_network(v.strip(), strict=False)
                                  for v in proxy_setting.split(",") if v.strip()]
        self.data_dir = Path(data_dir).resolve()
        self.data_dir.mkdir(parents=True, exist_ok=True)
        self.dbfile = self.data_dir / "hub.sqlite3"
        self.db_lock = threading.RLock()
        # One live SQLite connection prevents expensive open/close WAL
        # checkpoint cycles on slow virtual disks. All access is under db_lock.
        self._database = sqlite3.connect(
            str(self.dbfile),timeout=20,isolation_level="DEFERRED",
            check_same_thread=False)
        self._database.row_factory=sqlite3.Row
        self._database.execute("PRAGMA foreign_keys=ON")
        self._database.execute("PRAGMA journal_mode=WAL")
        self._database.execute("PRAGMA synchronous=FULL")
        self.state_lock = threading.Condition(threading.RLock())
        self.queued: dict[str, deque] = defaultdict(deque)
        self.pending: dict[str, dict] = {}
        self.presence: dict[str, dict] = {}
        self.throttle: dict[str, deque] = defaultdict(deque)
        self.throttle_lock = threading.Lock()
        self.catalogue = json.loads((Path(__file__).parent / "tools_manifest.json").read_text(encoding="utf-8"))
        self.tool_names = {tool["name"] for tool in self.catalogue}
        self._init_db()
        # Generated from public source only; the origin comes from ASC_HUB_URL.
        self.public_packages = build_distributions(
            Path(__file__).resolve().parent.parent, self.data_dir, self.base)
        trace_enabled = os.environ.get("ASC_HUB_TRACE_PAYLOADS", "0").strip().lower() in ("1", "true", "yes", "on")
        # A single opt-in switch enables complete MCP/agent traffic traces for
        # EVERY authenticated tenant. Files remain isolated by user and device.
        # No owner email or account lookup is necessary at startup.
        self.trace = TrafficTrace(
            self.data_dir, enabled=trace_enabled,
            max_bytes=int(os.environ.get("ASC_HUB_TRACE_MAX_MB", "64")) * 1024 * 1024,
            backups=int(os.environ.get("ASC_HUB_TRACE_BACKUPS", "4")),
        )

    @contextmanager
    def db(self, *, telemetry: bool = False):
        # The RLock also protects direct calls by tests/maintenance code.
        # SQLite's connection is not used from parallel threads concurrently.
        with self.db_lock:
            db=self._database
            # Last telemetry writes may be lost on power failure, while account
            # and authorization data use FULL synchronous durability.
            if telemetry:
                db.execute("PRAGMA synchronous=NORMAL")
            try:
                with db:
                    yield db
            finally:
                if telemetry:
                    db.execute("PRAGMA synchronous=FULL")

    def _init_db(self):
        with self.db_lock, self.db() as db:
            db.execute("PRAGMA journal_mode=WAL")
            db.executescript("""
                CREATE TABLE IF NOT EXISTS users (
                  id TEXT PRIMARY KEY, email TEXT NOT NULL UNIQUE, password_hash TEXT NOT NULL,
                  created REAL NOT NULL, is_admin INTEGER NOT NULL DEFAULT 0,
                  diagnostic INTEGER NOT NULL DEFAULT 0);
                CREATE TABLE IF NOT EXISTS sessions (
                  hash TEXT PRIMARY KEY, user_id TEXT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
                  csrf_token TEXT NOT NULL, expires REAL NOT NULL);
                CREATE TABLE IF NOT EXISTS devices (
                  id TEXT PRIMARY KEY, user_id TEXT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
                  name TEXT NOT NULL, token_hash TEXT NOT NULL UNIQUE, enabled INTEGER NOT NULL DEFAULT 1,
                  registered REAL NOT NULL, last_seen REAL NOT NULL DEFAULT 0, tools TEXT NOT NULL DEFAULT '[]',
                  info TEXT NOT NULL DEFAULT '{}');
                CREATE INDEX IF NOT EXISTS idx_devices_owner ON devices(user_id);
                CREATE TABLE IF NOT EXISTS pairings (
                  hash TEXT PRIMARY KEY, user_id TEXT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
                  expires REAL NOT NULL);
                CREATE TABLE IF NOT EXISTS oauth_clients (
                  id TEXT PRIMARY KEY, redirects TEXT NOT NULL,
                  client_name TEXT NOT NULL DEFAULT 'MCP application');
                CREATE TABLE IF NOT EXISTS oauth_requests (
                  id TEXT PRIMARY KEY, client_id TEXT NOT NULL, redirect TEXT NOT NULL,
                  challenge TEXT NOT NULL, resource TEXT NOT NULL, state TEXT NOT NULL,
                  scopes TEXT NOT NULL, expires REAL NOT NULL);
                CREATE TABLE IF NOT EXISTS oauth_codes (
                  hash TEXT PRIMARY KEY, user_id TEXT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
                  client_id TEXT NOT NULL, redirect TEXT NOT NULL, challenge TEXT NOT NULL,
                  resource TEXT NOT NULL, scopes TEXT NOT NULL, expires REAL NOT NULL);
                CREATE TABLE IF NOT EXISTS oauth_tokens (
                  hash TEXT PRIMARY KEY, user_id TEXT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
                  client_id TEXT NOT NULL, kind TEXT NOT NULL, resource TEXT NOT NULL,
                  scopes TEXT NOT NULL, expires REAL NOT NULL);
                CREATE TABLE IF NOT EXISTS usage_daily (
                  user_id TEXT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
                  device_id TEXT NOT NULL, day TEXT NOT NULL, tool TEXT NOT NULL,
                  calls INTEGER NOT NULL DEFAULT 0, errors INTEGER NOT NULL DEFAULT 0,
                  PRIMARY KEY (user_id,device_id,day,tool));
                CREATE TABLE IF NOT EXISTS diagnostics (
                  user_id TEXT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
                  created REAL NOT NULL, device_id TEXT NOT NULL,
                  tool TEXT NOT NULL, status TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS error_events (
                  id TEXT PRIMARY KEY,
                  user_id TEXT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
                  created REAL NOT NULL,
                  device_id TEXT NOT NULL,
                  tool TEXT NOT NULL,
                  error_type TEXT NOT NULL,
                  message TEXT NOT NULL,
                  request_id TEXT,
                  job_id TEXT,
                  source TEXT NOT NULL);
                CREATE INDEX IF NOT EXISTS idx_errors_owner_recent
                  ON error_events(user_id,created DESC);
                CREATE UNIQUE INDEX IF NOT EXISTS idx_errors_owner_request
                  ON error_events(user_id,request_id)
                  WHERE request_id IS NOT NULL;
                CREATE TABLE IF NOT EXISTS performance_events (
                  id INTEGER PRIMARY KEY AUTOINCREMENT,
                  user_id TEXT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
                  device_id TEXT NOT NULL, tool TEXT NOT NULL,
                  created REAL NOT NULL,
                  queue_ms REAL, agent_ms REAL, relay_ms REAL,
                  total_ms REAL NOT NULL, status TEXT NOT NULL);
                CREATE INDEX IF NOT EXISTS idx_perf_owner_recent
                  ON performance_events(user_id,created DESC);

            """)
        # Existing ChatGPT connections must survive a migration in place.
        with self.db_lock, self.db() as db:
            columns={row["name"] for row in db.execute("PRAGMA table_info(oauth_clients)")}
            if "client_name" not in columns:
                db.execute("ALTER TABLE oauth_clients ADD COLUMN client_name TEXT NOT NULL DEFAULT 'MCP application'")
        try:
            os.chmod(self.dbfile, 0o600)
        except OSError:
            pass

    def row(self, sql: str, params=()) -> dict | None:
        with self.db_lock, self.db() as db:
            entry = db.execute(sql, params).fetchone()
            return dict(entry) if entry else None

    def rows(self, sql: str, params=()) -> list[dict]:
        with self.db_lock, self.db() as db:
            return [dict(row) for row in db.execute(sql, params).fetchall()]

    def execute(self, sql: str, params=(), *, telemetry: bool = False):
        with self.db_lock, self.db(telemetry=telemetry) as db:
            return db.execute(sql, params).rowcount

    def cleanup_expired(self):
        """Remove expired credentials and consent-free diagnostics. No payloads retained."""
        cutoff=time.time()
        with self.db_lock, self.db() as db:
            for table,column in (("pairings","expires"),("oauth_requests","expires"),
                                 ("oauth_codes","expires"),("oauth_tokens","expires"),
                                 ("sessions","expires")):
                db.execute(f"DELETE FROM {table} WHERE {column}<?",(cutoff,))
            db.execute("DELETE FROM diagnostics WHERE created<?", (cutoff-7*86400,))
            db.execute("DELETE FROM error_events WHERE created<?", (cutoff-30*86400,))
            db.execute("DELETE FROM performance_events WHERE created<?", (cutoff-7*86400,))

    def throttle_ip(self, category: str, ip: str, maximum: int, window: int):
        # Applies to login, signup, pairing and OAuth client registration ONLY.
        # Tool invocations have NO quota and NO throttling.
        key = f"{category}:{ip}"
        now = time.monotonic()
        with self.throttle_lock:
            records = self.throttle[key]
            while records and now - records[0] > window:
                records.popleft()
            if len(records) >= maximum:
                raise Failure(429, "Too many authorization attempts; try again later")
            records.append(now)

    def register_user(self, email: str, password: str) -> dict:
        email = str(email).strip().lower()
        if not EMAIL_RE.fullmatch(email) or len(email) > 254:
            raise Failure(400, "Invalid email address")
        try:
            pwd = hash_password(password)
        except ValueError as e:
            raise Failure(400, str(e)) from e
        uid = "u_" + uuid.uuid4().hex
        try:
            self.execute("INSERT INTO users(id,email,password_hash,created) VALUES(?,?,?,?)",
                         (uid, email, pwd, time.time()))
        except sqlite3.IntegrityError:
            raise Failure(409, "Email address already registered")
        return {"id": uid, "email": email}

    def authenticate(self, email: str, password: str) -> dict | None:
        user = self.row("SELECT * FROM users WHERE email=?", (str(email).strip().lower(),))
        # Approximately constant login cost even if user does not exist.
        dummy = "pbkdf2-sha256$500000$" + "00" * 16 + "$" + "00" * 32
        valid = verify_password(str(password), user["password_hash"] if user else dummy)
        return user if valid else None

    def create_admin(self, email: str, password: str):
        item = self.register_user(email, password)
        self.execute("UPDATE users SET is_admin=1 WHERE id=?", (item["id"],))
        return item

    def session_create(self, uid: str) -> tuple[str, str]:
        token, csrf = secrets.token_urlsafe(48), secrets.token_urlsafe(32)
        self.execute("INSERT INTO sessions(hash,user_id,csrf_token,expires) VALUES(?,?,?,?)",
                     (digest(token), uid, csrf, time.time() + 86400 * 14))
        return token, csrf

    def _session_csrf(self, sid: str) -> str:
        entry = self.row("SELECT csrf_token FROM sessions WHERE hash=? AND expires>?", (digest(sid), time.time()))
        return entry["csrf_token"] if entry else ""

    def session_owner(self, bearer: str) -> dict | None:
        if len(bearer) < 25 or len(bearer) > 180:
            return None
        return self.row("""SELECT u.id,u.email,u.is_admin,u.diagnostic,s.csrf_token FROM sessions s
                           JOIN users u ON u.id=s.user_id WHERE s.hash=? AND s.expires>?""",
                        (digest(bearer), time.time()))

    def pairing_create(self, uid: str) -> str:
        code = secrets.token_urlsafe(32)
        self.execute("INSERT INTO pairings(hash,user_id,expires) VALUES(?,?,?)",
                     (digest(code), uid, time.time() + 600))
        return code

    def pairing_redeem(self, code: str, name: str, platform: str, tools: list) -> dict:
        if not isinstance(code, str) or len(code) > 256 or not isinstance(name, str) or not 1 <= len(name) <= 80:
            raise Failure(400, "Invalid enrollment data")
        if not isinstance(tools, list) or len(tools) > 60:
            raise Failure(400, "Invalid tool list")
        if not isinstance(platform, str) or len(platform) > 150:
            raise Failure(400, "Invalid platform")
        real_tools = [name for name in tools if isinstance(name, str) and name in self.tool_names]
        with self.db_lock, self.db() as db:
            item = db.execute("SELECT user_id,expires FROM pairings WHERE hash=?", (digest(code),)).fetchone()
            if not item or item["expires"] <= time.time():
                raise Failure(401, "Invalid or expired pairing code")
            db.execute("DELETE FROM pairings WHERE hash=?", (digest(code),))
            device_id, token = "d_" + uuid.uuid4().hex, secrets.token_urlsafe(48)
            db.execute("""INSERT INTO devices(id,user_id,name,token_hash,registered,tools,info)
                          VALUES(?,?,?,?,?,?,?)""",
                       (device_id, item["user_id"], name, digest(token), time.time(),
                        json.dumps(real_tools), json.dumps({"platform": platform[:150]})))
        return {"device_id": device_id, "device_token": token, "name": name}

    def user_device(self, uid: str, device_id: str) -> dict | None:
        return self.row("SELECT * FROM devices WHERE id=? AND user_id=? AND enabled=1", (device_id, uid))

    def list_devices(self, uid: str) -> list[dict]:
        devices = self.rows("SELECT id,name,registered,last_seen,tools,info FROM devices WHERE user_id=? AND enabled=1 ORDER BY registered", (uid,))
        result = []
        for d in devices:
            info = json.loads(d["info"])
            result.append({"id": d["id"], "name": d["name"], "online": time.time()-d["last_seen"] < 50,
                           "platform": info.get("platform", "unknown"),
                           "tools": json.loads(d["tools"]), "registered": int(d["registered"])})
        return result

    def agent_identity(self, device_id: str, token: str) -> dict:
        if not isinstance(device_id, str) or not isinstance(token, str) or len(token) < 32 or len(token) > 256:
            raise Failure(401, "Invalid agent credentials")
        device = self.row("SELECT * FROM devices WHERE id=? AND token_hash=? AND enabled=1", (device_id, digest(token)))
        if device is None:
            raise Failure(401, "Invalid agent credentials")
        return device

    def device_revoke(self, device: dict) -> dict:
        """Disable exactly the authenticated device and unblock its pending jobs."""
        did = device["id"]
        self.execute("UPDATE devices SET enabled=0 WHERE id=? AND user_id=?",
                     (did, device["user_id"]))
        with self.state_lock:
            self.queued.pop(did, None)
            for job in list(self.pending.values()):
                if job["device_id"] == did:
                    job["result"] = json_text({"error": "Device revoked"}, True)
                    job["event"].set()
            self.state_lock.notify_all()
        return {"revoked": True, "device_id": did}

    def agent_hello(self, device: dict, payload: dict) -> dict:
        machine = payload.get("machine_info", {})
        if not isinstance(machine, dict):
            machine = {}
        tools = payload.get("tools", [])
        if not isinstance(tools, list):
            tools = []
        safe_tools = sorted(set(x for x in tools if isinstance(x,str) and x in self.tool_names))
        info = {"platform": str(machine.get("platform", "unknown"))[:150],
                "hostname": str(machine.get("name", ""))[:80],
                 "version": str(payload.get("version") or machine.get("agent_version", ""))[:30]}
        self.execute("UPDATE devices SET tools=?,info=?,last_seen=? WHERE id=?",
                     (json.dumps(safe_tools),json.dumps(info),time.time(),device["id"]),telemetry=True)
        return {"ok": True, "device_id": device["id"]}

    def agent_poll(self, device: dict) -> dict:
        did = device["id"]
        self.execute("UPDATE devices SET last_seen=? WHERE id=?", (time.time(), did),telemetry=True)
        deadline = time.monotonic() + 18
        with self.state_lock:
            while True:
                while self.queued[did]:
                    job = self.queued[did].popleft()
                    if job["id"] in self.pending:
                        job["dispatched_at"]=time.monotonic()
                        return {"job": {"id": job["id"], "tool": job["tool"],
                                        "arguments": job["arguments"],
                                        "ttl_seconds":max(0,125-(time.monotonic()-job["enqueued_at"]))}}
                remain = deadline - time.monotonic()
                if remain <= 0:
                    return {"job": None}
                self.state_lock.wait(timeout=remain)

    def agent_result(self, device: dict, data: dict) -> dict:
        jobid = data.get("id", "")
        with self.state_lock:
            job = self.pending.get(jobid)
            if not job or job["device_id"] != device["id"]:
                raise Failure(404, "Unknown or expired job")
            if not isinstance(data.get("result"), dict):
                raise Failure(400, "Invalid tool result")
            job["result"] = data["result"]
            job["completed_at"]=time.monotonic()
            job["event"].set()
        self.execute("UPDATE devices SET last_seen=? WHERE id=?", (time.time(), device["id"]),telemetry=True)
        return {"ok": True}

    def track(self, uid: str, device_id: str, tool: str, error: bool, *,
              message: str = "", request_id: str | None = None,
              job_id: str | None = None):
        day = time.strftime("%Y-%m-%d", time.gmtime())
        self.execute("""INSERT INTO usage_daily(user_id,device_id,day,tool,calls,errors)
                    VALUES(?,?,?,?,1,?) ON CONFLICT(user_id,device_id,day,tool)
                    DO UPDATE SET calls=calls+1,errors=errors+excluded.errors""",
                     (uid,device_id,day,tool,int(error)),telemetry=True)
        if error:
            detail = message if isinstance(message,str) else "Ошибка выполнения"
            detail = detail[:3000] or "Инструмент вернул ошибку без описания"
            first, separator, rest = detail.partition(":")
            kind = first if separator and re.fullmatch(r"[A-Za-z][A-Za-z0-9_.]{0,70}",first) else "ToolError"
            self.execute("""INSERT OR IGNORE INTO error_events
                         (id,user_id,created,device_id,tool,error_type,message,request_id,job_id,source)
                         VALUES(?,?,?,?,?,?,?,?,?,?)""",
                         ("err_"+uuid.uuid4().hex,uid,time.time(),device_id,str(tool)[:100],
                          kind,detail,request_id,job_id,"agent" if device_id!="hub" else "hub"))
        owner = self.row("SELECT diagnostic FROM users WHERE id=?", (uid,))
        if owner and owner["diagnostic"]:
            self.execute("INSERT INTO diagnostics(user_id,created,device_id,tool,status) VALUES(?,?,?,?,?)",
                         (uid,time.time(),device_id,tool,"error" if error else "ok"),telemetry=True)


    def performance_record(self, uid: str, job: dict, finished: float):
        # Monotonic timestamps avoid clock changes during long-running jobs.
        start=job.get("enqueued_at",finished)
        dispatch=job.get("dispatched_at")
        done=job.get("completed_at")
        q=max(0,(dispatch-start)*1000) if dispatch is not None else None
        a=max(0,(done-dispatch)*1000) if dispatch is not None and done is not None else None
        relay=max(0,(finished-done)*1000) if done is not None else None
        self.execute("""INSERT INTO performance_events(user_id,device_id,tool,created,
                       queue_ms,agent_ms,relay_ms,total_ms,status)
                       VALUES(?,?,?,?,?,?,?,?,?)""",
                     (uid,job["device_id"],job["tool"],time.time(),
                      q,a,relay,max(0,(finished-start)*1000),
                      job.get("status","interrupted")),telemetry=True)

    @staticmethod
    def _percentile(values: list[float], portion: float) -> float | None:
        if not values:return None
        v=sorted(values)
        return round(v[min(len(v)-1,int((len(v)-1)*portion))],1)

    def performance_summary(self, uid: str, *, hours: int = 24,
                            device: str = "") -> dict:
        if not isinstance(hours,int) or not 1<=hours<=168:
            raise Failure(400,"hours must be in 1..168")
        if device and not self.user_device(uid,device):
            raise Failure(404,"Device not found")
        sql="""SELECT device_id,tool,queue_ms,agent_ms,relay_ms,total_ms,status
               FROM performance_events WHERE user_id=? AND created>?"""
        args=[uid,time.time()-hours*3600]
        if device:
            sql+=" AND device_id=?"
            args.append(device)
        sql+=" ORDER BY created DESC LIMIT 25000"
        groups=defaultdict(list)
        for record in self.rows(sql,args):
            groups[(record["device_id"],record["tool"])].append(record)
        stats=[]
        for (did,tool),rows in sorted(groups.items()):
            def aggregate(key):
                values=[float(x[key]) for x in rows if x[key] is not None]
                return {"p50_ms":self._percentile(values,.5),
                        "p95_ms":self._percentile(values,.95),
                        "max_ms":round(max(values),1) if values else None}
            stats.append({"device_id":did,"tool":tool,"calls":len(rows),
                          "errors":sum(x["status"]!="ok" for x in rows),
                          "queue":aggregate("queue_ms"),"agent":aggregate("agent_ms"),
                          "relay":aggregate("relay_ms"),"total":aggregate("total_ms")})
        return {"hours":hours,"device":device,"groups":stats,
                "sample_count":sum(len(g) for g in groups.values()),
                "retention_days":7,"truncated":sum(len(g) for g in groups.values())>=25000}

    @staticmethod
    def error_text(result: dict) -> str:
        if not isinstance(result,dict):
            return "Неверный ответ устройства"
        for entry in result.get("content",[]):
            if isinstance(entry,dict) and entry.get("type")=="text":
                value=entry.get("text","")
                if isinstance(value,str) and value.strip():
                    return value.strip()
        return "Устройство сообщило об ошибке без описания"

    def errors(self, uid: str, *, device: str = "", tool: str = "",
               days: int = 30, limit: int = 25, offset: int = 0) -> dict:
        if not isinstance(days,int) or not 1 <= days <= 30:
            raise Failure(400,"Invalid days range")
        if not isinstance(limit,int) or not 1 <= limit <= 100:
            raise Failure(400,"Invalid page size")
        if not isinstance(offset,int) or not 0 <= offset <= 5000:
            raise Failure(400,"Invalid page offset")
        if device and not self.row("SELECT id FROM devices WHERE id=? AND user_id=?",(device,uid)):
            raise Failure(404,"Device not found")
        if tool and (not isinstance(tool,str) or re.fullmatch(r"[a-zA-Z0-9_.-]{1,100}",tool) is None):
            raise Failure(400,"Invalid tool filter")
        cutoff=time.time()-days*86400
        filters=["e.user_id=?","e.created>=?"]
        params=[uid,cutoff]
        if device:
            filters.append("e.device_id=?")
            params.append(device)
        if tool:
            filters.append("e.tool=?")
            params.append(tool)
        where=" AND ".join(filters)
        total=self.row("SELECT COUNT(*) AS count FROM error_events e WHERE "+where,params)["count"]
        events=self.rows("""SELECT e.id,e.created,e.device_id,
                                   COALESCE(d.name,'Хаб') AS device_name,
                                   e.tool,e.error_type,e.message,e.request_id,
                                   e.job_id,e.source
                            FROM error_events e
                            LEFT JOIN devices d ON d.id=e.device_id AND d.user_id=e.user_id
                            WHERE """+where+"""
                            ORDER BY e.created DESC,e.id DESC LIMIT ? OFFSET ?""",
                            [*params,limit,offset])
        return {"events":events,"total":total,"offset":offset,"limit":limit,
                "has_more":offset+len(events)<total,"retention_days":30}

    def error_trace(self, uid: str, event_id: str) -> dict:
        if re.fullmatch(r"err_[0-9a-f]{32}",event_id) is None:
            raise Failure(404,"Error event not found")
        entry=self.row("SELECT * FROM error_events WHERE id=? AND user_id=?",(event_id,uid))
        if not entry:
            raise Failure(404,"Error event not found")
        if not self.trace.enabled:
            return {"available":False,"reason":"Полная трассировка отключена",
                    "records":[]}
        device_id=entry["device_id"]
        if device_id!="hub" and not self.row("SELECT id FROM devices WHERE id=? AND user_id=?",
                                              (device_id,uid)):
            raise Failure(404,"Device not found")
        # Never use request-supplied paths. The journal path is derived only
        # from a validated owner and a server-side device ID.
        paths=[self.trace.path_for(uid,None if device_id=="hub" else device_id)]
        records=[]
        total_bytes=0
        limited=False
        for path in paths:
            sources=[path.with_name(path.name+"."+str(i))
                     for i in range(self.trace.backups,0,-1)]+[path]
            for source in sources:
                if not source.is_file() or source.is_symlink():
                    continue
                with source.open("r",encoding="utf-8") as stream:
                    for line in stream:
                        try:
                            item=json.loads(line)
                        except (ValueError,TypeError):
                            continue
                        # Include only matching request and its corresponding
                        # job envelope (if available) from the owner's device.
                        trace_match=entry["request_id"] and item.get("request_id")==entry["request_id"]
                        try:
                            payload=self.trace.decode_payload(item.get("payload"))
                        except (ValueError,KeyError,TypeError,OverflowError):
                            continue
                        item["payload"]=payload
                        job_match=(entry["job_id"] and isinstance(payload,dict) and
                                   (payload.get("id")==entry["job_id"] or
                                    isinstance(payload.get("job"),dict) and
                                    payload["job"].get("id")==entry["job_id"]))
                        if not (trace_match or job_match):
                            continue
                        if item.get("user_id")!=uid:
                            continue
                        encoded=json.dumps(item,ensure_ascii=False)
                        if total_bytes+len(encoded.encode("utf-8"))>2_000_000 or len(records)>=30:
                            limited=True
                            continue
                        total_bytes+=len(encoded.encode("utf-8"))
                        records.append(item)
        return {"available":bool(records),"records":records,
                "limited":limited,"trace_enabled":True}

    def usage(self, uid: str, days: int = 30) -> dict:
        start = time.strftime("%Y-%m-%d", time.gmtime(time.time()-min(max(days,1),365)*86400))
        rows = self.rows("""SELECT day,device_id,tool,calls,errors FROM usage_daily
                          WHERE user_id=? AND day>=? ORDER BY day DESC,calls DESC LIMIT 3000""", (uid,start))
        total = self.row("SELECT COALESCE(SUM(calls),0) AS total,COALESCE(SUM(errors),0) AS errors FROM usage_daily WHERE user_id=?", (uid,))
        return {"total": total["total"], "errors": total["errors"], "daily": rows,
                "usage_limit": None, "remaining": None}

    def admin_summary(self) -> dict:
        return {"users": self.row("SELECT COUNT(*) AS count FROM users")["count"],
                "devices": self.row("SELECT COUNT(*) AS count FROM devices WHERE enabled=1")["count"],
                "online_devices": self.row("SELECT COUNT(*) AS count FROM devices WHERE enabled=1 AND last_seen>?", (time.time()-50,))["count"],
                "tool_calls": self.row("SELECT COALESCE(SUM(calls),0) AS count FROM usage_daily")["count"],
                "tool_errors": self.row("SELECT COALESCE(SUM(errors),0) AS count FROM usage_daily")["count"]}

    # OAuth 2.1: PKCE S256, one-time authorization codes, rotating refresh tokens.
    # Every access/refresh token is bound to exactly one user AND one MCP resource.
    def oauth_metadata(self) -> dict:
        return {"issuer": self.base,
                "authorization_endpoint": self.base+"/oauth/authorize",
                "token_endpoint": self.base+"/oauth/token",
                "registration_endpoint": self.base+"/oauth/register",
                "response_types_supported": ["code"],
                "grant_types_supported": ["authorization_code", "refresh_token"],
                "token_endpoint_auth_methods_supported": ["none"],
                "code_challenge_methods_supported": ["S256"],
                "scopes_supported": ["remote.access", "offline_access"],
                "authorization_response_iss_parameter_supported": True,
                "client_id_metadata_document_supported": False}

    def resource_metadata(self) -> dict:
        return {"resource": self.resource, "authorization_servers": [self.base],
                "scopes_supported": ["remote.access"]}

    def oauth_register(self, params: dict) -> dict:
        # RFC 7591 public-client DCR. Do not whitelist providers by brand.
        # PKCE + redirect equality + user consent provide the security boundary.
        redirects=params.get("redirect_uris",[])
        if (not isinstance(redirects,list) or not 1<=len(redirects)<=12 or
                not all(valid_oauth_redirect(u) for u in redirects) or
                len(set(redirects))!=len(redirects)):
            raise Failure(400,"Invalid OAuth redirect URIs: use HTTPS or a loopback HTTP callback with an explicit port")
        if params.get("token_endpoint_auth_method","none")!="none":
            raise Failure(400,"Public OAuth clients with PKCE are required")
        grants=params.get("grant_types",["authorization_code","refresh_token"])
        responses=params.get("response_types",["code"])
        if (not isinstance(grants,list) or not grants or
                not all(isinstance(item,str) for item in grants) or
                set(grants)-{"authorization_code","refresh_token"} or
                "authorization_code" not in grants or
                not isinstance(responses,list) or responses!=["code"]):
            raise Failure(400,"Only authorization_code, refresh_token and code response type supported")
        display=params.get("client_name","MCP application")
        if (not isinstance(display,str) or not 1<=len(display.strip())<=100 or
                any(ord(c)<32 or ord(c)==127 for c in display)):
            raise Failure(400,"Invalid OAuth client_name")
        client="c_"+secrets.token_urlsafe(30)
        display=display.strip()
        self.execute("INSERT INTO oauth_clients(id,redirects,client_name) VALUES(?,?,?)",
                     (client,json.dumps(redirects),display))
        return {"client_id":client,"client_id_issued_at":int(time.time()),
                "client_name":display,"redirect_uris":redirects,
                "token_endpoint_auth_method":"none",
                "grant_types":["authorization_code","refresh_token"],
                "response_types":["code"]}


    def authorize(self, fields: dict) -> str:
        client_id = fields.get("client_id", "")
        record = self.row("SELECT redirects,client_name FROM oauth_clients WHERE id=?", (client_id,))
        redirect = fields.get("redirect_uri", "")
        if not record or redirect not in json.loads(record["redirects"]):
            raise Failure(400, "Unknown OAuth client or redirect")
        challenge = fields.get("code_challenge", "")
        if fields.get("response_type") != "code" or fields.get("code_challenge_method") != "S256" or not PKCE_RE.fullmatch(challenge):
            raise Failure(400, "PKCE S256 required")
        scope = fields.get("scope", "remote.access")
        if "remote.access" not in scope.split() or any(s not in ("remote.access","offline_access") for s in scope.split()):
            raise Failure(400, "Invalid scope")
        if fields.get("resource",self.resource) not in (self.resource,self.base):
            raise Failure(400, "OAuth resource mismatch")
        state = fields.get("state", "")
        if not isinstance(state, str) or len(state)>1024:
            raise Failure(400, "Invalid state")
        ticket = secrets.token_urlsafe(32)
        self.execute("""INSERT INTO oauth_requests(id,client_id,redirect,challenge,resource,state,scopes,expires)
                      VALUES(?,?,?,?,?,?,?,?)""",
                     (ticket, client_id, redirect, challenge, self.resource,state,scope,time.time()+900))
        t = html.escape(ticket,quote=True)
        action = html.escape(self.base+"/oauth/approve", quote=True)
        client_label=html.escape(record["client_name"],quote=True)
        redirect_host=html.escape(urlparse(redirect).netloc,quote=True)
        return f"""<!doctype html><html lang="ru"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
        <title>Подключение MCP-приложения — ASC Remote Hub</title><link rel="stylesheet" href="/static/style.css"></head>
        <body><main class="oauth"><h1>Разрешить подключение MCP-приложения</h1>
        <p>Авторизуйся в <b>своей</b> учётной записи ASC Remote Hub. Приложение <b>{client_label}</b> запрашивает доступ к устройствам твоего аккаунта в пределах разрешений агентов. Адрес возврата: <b>{redirect_host}</b>. Название указано самим приложением и не подтверждает его подлинность.</p>
        <form method="post" action="{action}"><input name="ticket" type="hidden" value="{t}">
        <label>Email<input name="email" type="email" required autocomplete="username"></label>
        <label>Пароль<input name="password" type="password" required autocomplete="current-password"></label>
        <label class="check"><input type="checkbox" name="consent" value="yes" required> Я разрешаю подключить это MCP-приложение к моей учётной записи</label>
        <button type="submit">Разрешить подключение</button></form>
        <p class="sub">Форма действительна 15 минут <strong>с момента открытия страницы</strong>,
        а не с момента ввода пароля. Если вкладка была открыта ранее, запусти новое подключение из MCP-приложения.</p>
        <p>Нет учётной записи? <a href="/">Зарегистрируйся бесплатно</a>, затем вернись к этой странице.</p>
        </main></body></html>"""

    def oauth_approve(self, fields: dict, ip: str) -> str:
        self.throttle_ip("oauth_login",ip,18,300)
        ticket = fields.get("ticket", "")
        if fields.get("consent") != "yes":
            raise Failure(403, "Explicit consent required")
        if not isinstance(ticket,str) or len(ticket)>180:
            raise Failure(403, "Invalid authorization request")
        pending = self.row("SELECT * FROM oauth_requests WHERE id=?", (ticket,))
        if pending is None:
            # Either the original form has already been submitted or an old
            # browser tab was used. Never accept a spent ticket a second time.
            print("oauth_consent_rejected reason=missing_or_reused_ticket",flush=True)
            raise Failure(403, "Authorization form already used or no longer exists. Start sign-in again from the MCP application; do not resubmit an old browser tab.")
        if pending["expires"] < time.time():
            print("oauth_consent_rejected reason=expired_ticket",flush=True)
            raise Failure(403, "Authorization form expired after 15 minutes. Return to your MCP application and start sign-in again.")
        user = self.authenticate(fields.get("email",""),fields.get("password",""))
        if not user:
            raise Failure(401, "Invalid email or password")
        code = secrets.token_urlsafe(48)
        with self.db_lock, self.db() as db:
            deleted = db.execute("DELETE FROM oauth_requests WHERE id=?",(ticket,)).rowcount
            if deleted != 1:
                raise Failure(403,"Authorization already used")
            db.execute("""INSERT INTO oauth_codes(hash,user_id,client_id,redirect,challenge,resource,scopes,expires)
                         VALUES(?,?,?,?,?,?,?,?)""",
                      (digest(code),user["id"],pending["client_id"],pending["redirect"],
                       pending["challenge"],pending["resource"],pending["scopes"],time.time()+180))
        sep = "&" if "?" in pending["redirect"] else "?"
        return pending["redirect"]+sep+urlencode({"code":code,"state":pending["state"],"iss":self.base})

    def _mint(self, db, uid: str, client: str, resource: str, scopes: str) -> dict:
        access,refresh = secrets.token_urlsafe(48),secrets.token_urlsafe(48)
        db.execute("INSERT INTO oauth_tokens(hash,user_id,client_id,kind,resource,scopes,expires) VALUES(?,?,?,?,?,?,?)",
                   (digest(access),uid,client,"access",resource,scopes,time.time()+3600))
        db.execute("INSERT INTO oauth_tokens(hash,user_id,client_id,kind,resource,scopes,expires) VALUES(?,?,?,?,?,?,?)",
                   (digest(refresh),uid,client,"refresh",resource,scopes,time.time()+86400*90))
        return {"access_token":access,"token_type":"Bearer","expires_in":3600,
                "refresh_token":refresh,"scope":scopes}

    def oauth_token(self, form: dict) -> dict:
        client = form.get("client_id", "")
        if self.row("SELECT id FROM oauth_clients WHERE id=?",(client,)) is None:
            raise Failure(401,"Invalid OAuth client")
        kind = form.get("grant_type")
        if kind == "authorization_code":
            code = form.get("code","")
            verifier = form.get("code_verifier","")
            if not isinstance(code,str) or not isinstance(verifier,str) or not 43<=len(verifier)<=128 or not re.fullmatch(r"[A-Za-z0-9._~\-]{43,128}",verifier):
                raise Failure(400,"Invalid code or PKCE verifier")
            with self.db_lock, self.db() as db:
                row = db.execute("SELECT * FROM oauth_codes WHERE hash=?",(digest(code),)).fetchone()
                if row is None:
                    raise Failure(400,"Invalid or expired authorization code")
                db.execute("DELETE FROM oauth_codes WHERE hash=?",(digest(code),))
                if (row["expires"] <= time.time() or row["client_id"]!=client or
                    row["redirect"]!=form.get("redirect_uri","") or
                    not hmac.compare_digest(pkce(verifier),row["challenge"]) or
                    form.get("resource",row["resource"]) not in (row["resource"],self.base)):
                    raise Failure(400,"Invalid OAuth code, PKCE or resource")
                return self._mint(db,row["user_id"],client,row["resource"],row["scopes"])
        if kind == "refresh_token":
            refresh = form.get("refresh_token","")
            if not isinstance(refresh,str) or not 20<len(refresh)<=180:
                raise Failure(400,"Invalid refresh token")
            with self.db_lock, self.db() as db:
                row = db.execute("SELECT * FROM oauth_tokens WHERE hash=? AND kind='refresh'",(digest(refresh),)).fetchone()
                if row is None:
                    raise Failure(400,"Invalid refresh token")
                db.execute("DELETE FROM oauth_tokens WHERE hash=?",(digest(refresh),))
                if row["expires"] <= time.time() or row["client_id"]!=client or form.get("resource",row["resource"]) not in (row["resource"],self.base):
                    raise Failure(400,"Invalid refresh token or resource")
                return self._mint(db,row["user_id"],client,row["resource"],row["scopes"])
        raise Failure(400,"Unsupported OAuth grant")

    def oauth_connections(self, uid: str) -> dict:
        """List only clients with active credentials belonging to this user."""
        rows=self.rows("""SELECT c.id AS client_id, c.client_name, c.redirects,
                                 MAX(t.expires) AS expires
                          FROM oauth_tokens t
                          JOIN oauth_clients c ON c.id=t.client_id
                          WHERE t.user_id=? AND t.expires>?
                          GROUP BY c.id,c.client_name,c.redirects
                          ORDER BY c.client_name,c.id LIMIT 100""",
                       (uid,time.time()))
        for row in rows:
            row["callback_hosts"]=sorted({urlparse(uri).netloc for uri in
                                          json.loads(row.pop("redirects"))})
        return {"connections":rows}

    def oauth_revoke_connection(self, uid: str, client_id: str) -> bool:
        if not isinstance(client_id,str) or len(client_id)>128:
            return False
        with self.db_lock, self.db() as db:
            exists=db.execute("SELECT 1 FROM oauth_tokens WHERE user_id=? AND client_id=? LIMIT 1",
                              (uid,client_id)).fetchone()
            if exists is None:
                return False
            db.execute("DELETE FROM oauth_tokens WHERE user_id=? AND client_id=?",
                       (uid,client_id))
            db.execute("DELETE FROM oauth_codes WHERE user_id=? AND client_id=?",
                       (uid,client_id))
        return True

    def oauth_owner(self, bearer: str) -> dict | None:
        if not isinstance(bearer,str) or not 20<len(bearer)<=256:
            return None
        return self.row("""SELECT u.id,u.email,u.is_admin FROM oauth_tokens t
                           JOIN users u ON t.user_id=u.id WHERE t.hash=? AND t.kind='access'
                           AND t.resource=? AND t.expires>? AND instr(t.scopes,'remote.access')>0""",
                        (digest(bearer),self.resource,time.time()))

    def mcp_tools(self) -> list[dict]:
        core = [
            {"name":"list_devices","description":"List ONLY your own paired devices with IDs, online status and available tools.",
             "inputSchema":{"type":"object","properties":{},"additionalProperties":False},
             "annotations":{"readOnlyHint":True},
             "securitySchemes":[{"type":"oauth2","scopes":["remote.access"]}]},
            {"name":"usage_stats","description":"Get your own tool-use count and error count; no limits are imposed.",
             "inputSchema":{"type":"object","properties":{},"additionalProperties":False},
             "annotations":{"readOnlyHint":True},
             "securitySchemes":[{"type":"oauth2","scopes":["remote.access"]}]},
            {"name":"performance_stats","description":"Per-device tool latency percentiles (queue, execution, relay, total) for your own account.",
             "inputSchema":{"type":"object","properties":{
                 "hours":{"type":"integer","minimum":1,"maximum":168},
                 "device":{"type":"string","description":"Optional own device ID"}
             },"additionalProperties":False},
             "annotations":{"readOnlyHint":True},
             "securitySchemes":[{"type":"oauth2","scopes":["remote.access"]}]},
            {"name":"get_profile","description":"Return the current authenticated ASC Remote Hub account profile.",
             "inputSchema":{"type":"object","properties":{},"additionalProperties":False},
             "outputSchema":{"type":"object","properties":{"id":{"type":"string"},
                              "email":{"type":"string"}},"required":["id"],
                              "additionalProperties":False},
             "annotations":{"readOnlyHint":True,"destructiveHint":False,"openWorldHint":False},
             "securitySchemes":[{"type":"oauth2","scopes":["remote.access"]}],
             "_meta":{"openai/profile":True}},
        ]
        return core+self.catalogue

    def forward(self, uid: str, name: str, args: dict,
                request_id: str | None = None) -> dict:
        if name == "list_devices":
            self.track(uid,"hub",name,False)
            return json_text({"devices":self.list_devices(uid)})
        if name == "usage_stats":
            self.track(uid,"hub",name,False)
            return json_text(self.usage(uid))
        if name == "performance_stats":
            self.track(uid,"hub",name,False)
            return json_text(self.performance_summary(
                uid,hours=args.get("hours",24),device=args.get("device","")))
        if name == "get_profile":
            account=self.row("SELECT id,email FROM users WHERE id=?",(uid,))
            profile={"id":account["id"],"email":account["email"]}
            self.track(uid,"hub",name,False)
            return {**json_text(profile),"structuredContent":profile}
        if name not in self.tool_names:
            self.track(uid,"hub",str(name)[:100],True,
                       message="Unknown tool",request_id=request_id)
            return json_text({"error":"Unknown tool"},True)
        if not isinstance(args,dict):
            self.track(uid,"hub",name,True,message="Arguments must be an object",
                       request_id=request_id)
            return json_text({"error":"Arguments must be an object"},True)
        did=args.get("device","")
        if not isinstance(did,str) or len(did)>100:
            self.track(uid,"hub",name,True,
                       message="Device ID required; call list_devices first",
                       request_id=request_id)
            return json_text({"error":"Device ID required; call list_devices first"},True)
        d=self.user_device(uid,did)  # MANDATORY tenant guard
        if d is None:
            self.track(uid,"hub",name,True,
                       message="Device not found in your account",
                       request_id=request_id)
            return json_text({"error":"Device not found in your account"},True)
        if time.time()-d["last_seen"]>50:
            self.track(uid,did,name,True,message="Device is offline",
                       request_id=request_id)
            return json_text({"error":"Device is offline"},True)
        if name not in json.loads(d["tools"]):
            self.track(uid,did,name,True,
                       message="Tool is not enabled by this device",
                       request_id=request_id)
            return json_text({"error":"Tool is not enabled by this device"},True)
        with self.state_lock:
            if len(self.queued[did])>=256:
                self.track(uid,did,name,True,message="Device queue full",request_id=request_id)
                return json_text({"error":"Device queue full"},True)
            jobid=str(uuid.uuid4())
            event=threading.Event()
            job={"id":jobid,"event":event,"device_id":did,"user_id":uid,"tool":name,
                 "arguments":{k:v for k,v in args.items() if k!="device"},"result":None,
                 "enqueued_at":time.monotonic(),"status":"timeout"}
            self.pending[jobid]=job
            self.queued[did].append(job)
            self.state_lock.notify_all()
        try:
            if not event.wait(timeout=125):
                self.track(uid,did,name,True,
                           message="Timed out while waiting for device",
                           request_id=request_id,job_id=jobid)
                return json_text({"error":"Timed out while waiting for device"},True)
            result=job["result"]
            if not isinstance(result,dict) or not isinstance(result.get("content"),list):
                self.track(uid,did,name,True,
                           message="Invalid device response",
                           request_id=request_id,job_id=jobid)
                return json_text({"error":"Invalid device response"},True)
            failed=bool(result.get("isError"))
            job["status"]="tool_error" if failed else "ok"
            self.track(uid,did,name,failed,
                       message=self.error_text(result) if failed else "",
                       request_id=request_id if failed else None,
                       job_id=jobid if failed else None)
            return result
        finally:
            finished=time.monotonic()
            with self.state_lock:
                self.pending.pop(jobid,None)
                self.queued[did]=deque(x for x in self.queued[did] if x["id"]!=jobid)
            self.performance_record(uid,job,finished)

    def mcp(self, owner: dict, request: dict,
            request_id: str | None = None) -> dict | None:
        method=request.get("method")
        rid=request.get("id")
        if not isinstance(method,str):
            raise Failure(400,"Invalid MCP method")
        if rid is None:
            return None  # notifications/initialized
        if method=="initialize":
            params=request.get("params") or {}
            requested=params.get("protocolVersion","2025-11-25") if isinstance(params,dict) else "2025-11-25"
            response={"protocolVersion": requested if requested in PROTOCOLS else "2025-11-25",
                      "capabilities":{"tools":{"listChanged":False}},
                      "serverInfo":{"name":"ASC Remote Hub","version":VERSION},
                      "instructions":"Devices belong exclusively to the signed-in account. Call list_devices first; pass the exact device ID to tools. Confirm destructive changes. Your tool-use count is unlimited."}
        elif method=="ping":
            response={}
        elif method=="tools/list":
            response={"tools":self.mcp_tools()}
        elif method=="tools/call":
            params=request.get("params") or {}
            response=self.forward(owner["id"],params.get("name",""),
                                  params.get("arguments") or {},
                                  request_id=request_id)
        else:
            return {"jsonrpc":"2.0","id":rid,"error":{"code":-32601,"message":"Method not found"}}
        return {"jsonrpc":"2.0","id":rid,"result":response}


class Handler(BaseHTTPRequestHandler):
    server_version = "ASC-Remote-Hub/"+VERSION

    @property
    def hub(self) -> Hub:
        return self.server.hub  # type: ignore[attr-defined]

    def log_message(self, fmt, *args):
        # Never log URL query strings, bodies, passwords, OAuth codes or agent commands.
        if os.environ.get("ASC_HUB_LOG_REQUESTS")=="1":
            print("http", self.client_ip, self.command,self.route,flush=True)

    @property
    def route(self):
        return urlparse(self.path).path

    def trace_begin(self, user_id: str, device_id: str | None, payload: dict):
        # Never trace unauthenticated requests, login, OAuth, or device enrollment.
        if not self.hub.trace.enabled:
            return
        # MCP has no X-ASC-Device header. Derive the device from authenticated
        # tools/call arguments and validate tenant ownership before choosing
        # the destination journal. Other MCP methods stay in the hub bucket.
        if self.route == "/mcp" and device_id is None:
            if payload.get("method") == "tools/call":
                params = payload.get("params")
                params = params if isinstance(params, dict) else {}
                if params.get("name") not in ("list_devices", "usage_stats", "get_profile"):
                    args = params.get("arguments")
                    args = args if isinstance(args, dict) else {}
                    candidate = args.get("device")
                    if isinstance(candidate, str) and self.hub.user_device(user_id, candidate):
                        device_id = candidate
        self._trace_ctx = {
            "request_id": uuid.uuid4().hex,
            "route": self.route,
            "user_id": user_id,
            "device_id": device_id,
        }
        self.hub.trace.record(
            direction="chatgpt_to_hub" if self.route=="/mcp" else "agent_to_hub",
            payload=payload,
            **self._trace_ctx,
        )

    def send(self, status: int, content: dict | str | bytes | None = None,
             headers: dict | None = None, mimetype: str = "application/json"):
        if content is None:
            payload = b""
        elif isinstance(content,bytes):
            payload=content
        elif mimetype=="application/json":
            payload=json.dumps(content,ensure_ascii=False,separators=(",",":")).encode("utf-8")
        else:
            payload=str(content).encode("utf-8")
        # Persist the response BEFORE sending it over the socket, so a client
        # cannot receive a tool result before the corresponding trace exists.
        # Authentication headers/tokens are deliberately not captured.
        context = getattr(self, "_trace_ctx", None)
        if context is not None:
            self.hub.trace.record(
                direction="hub_to_chatgpt" if context["route"]=="/mcp" else "hub_to_agent",
                payload=content, status=status, **context,
            )
        self.send_response(status)
        self.send_header("Content-Type",mimetype+("; charset=utf-8" if mimetype.startswith("text/") or mimetype=="application/json" else ""))
        self.send_header("Content-Length",str(len(payload)))
        self.send_header("Cache-Control","no-store")
        self.send_header("X-Content-Type-Options","nosniff")
        self.send_header("Referrer-Policy","no-referrer")
        self.send_header("X-Frame-Options","DENY")
        if self.route in CORS_PUBLIC_PATHS:
            # These endpoints NEVER use session cookies for authentication.
            self.send_header("Access-Control-Allow-Origin", "*")
            if self.route == "/mcp":
                self.send_header("Access-Control-Expose-Headers", "WWW-Authenticate, MCP-Session-Id")
        if mimetype=="text/html":
            # Chromium enforces form-action against redirects after a POST,
            # including our OAuth callback 302. The authorization handler
            # validates the redirect against the registered client's exact URI
            # before storing this origin. Never weaken to form-action *.
            form_target = (
                self.hub.base+"/oauth/approve "+self._oauth_callback_origin
                if self.route == "/oauth/authorize" else "'self'"
            )
            self.send_header("Content-Security-Policy",
                "default-src 'self'; style-src 'self'; script-src 'self'; "
                "form-action "+form_target+"; frame-ancestors 'none'; base-uri 'none'")
        if not self.hub.test_mode:
            self.send_header("Strict-Transport-Security","max-age=31536000")
        for key,value in (headers or {}).items():
            self.send_header(key,value)
        self.end_headers()
        try:
            if payload:
                self.wfile.write(payload)
        except (ConnectionError,BrokenPipeError):
            pass

    @property
    def client_ip(self) -> str:
        peer=self.client_address[0]
        try:
            if not any(ipaddress.ip_address(peer) in net for net in self.hub.trusted_proxies):
                return peer
            forwarded=ipaddress.ip_address(self.headers.get("X-Real-IP","").strip())
            return str(forwarded)
        except ValueError:
            return peer

    def require_host(self):
        if not self.hub.test_mode and self.headers.get("Host")!=urlparse(self.hub.base).netloc:
            raise Failure(421,"Unexpected HTTP Host")
        origin=self.headers.get("Origin", "")
        if origin and origin != self.hub.base:
            # Cross-origin OAuth registration / token exchange and bearer MCP
            # traffic are protocol operations, not cookie-authenticated actions.
            # The authorization HTML is a top-level navigation, not a CORS API.
            # OAuth consent is a form navigation with an explicit one-time
            # ticket + account password + consent, NOT a cookie-authenticated
            # endpoint. Browser/webview form origins can differ.
            # Do not enable CORS or cross-origin cookies for this endpoint.
            if self.route not in CORS_PUBLIC_PATHS and self.route not in ("/oauth/authorize", "/oauth/approve"):
                raise Failure(403,"Cross-origin request rejected")

    def body(self, maximum=MAX_JSON) -> bytes:
        try:
            length=int(self.headers.get("Content-Length","0"))
        except ValueError:
            raise Failure(411,"Content-Length required")
        if length<1 or length>maximum:
            raise Failure(413,"Invalid request body length")
        payload=self.rfile.read(length)
        if len(payload)!=length:
            raise Failure(400,"Truncated request")
        return payload

    def jsonbody(self, maximum=MAX_JSON) -> dict:
        if "application/json" not in self.headers.get("Content-Type",""):
            raise Failure(415,"Expected application/json")
        try:
            obj=json.loads(self.body(maximum))
        except (UnicodeError,ValueError):
            raise Failure(400,"Invalid JSON")
        if not isinstance(obj,dict):
            raise Failure(400,"Expected JSON object")
        return obj

    def formbody(self) -> dict:
        if "application/x-www-form-urlencoded" not in self.headers.get("Content-Type",""):
            raise Failure(415,"Expected form-urlencoded")
        try:
            return {k:v[0] for k,v in parse_qs(self.body(MAX_FORM).decode("utf-8"),keep_blank_values=True).items()}
        except UnicodeError:
            raise Failure(400,"Invalid UTF-8 form")

    def bearer(self) -> str:
        auth=self.headers.get("Authorization","")
        if not auth.startswith("Bearer ") or len(auth)>600:
            return ""
        return auth[7:]

    def session(self, required: bool = True) -> tuple[dict | None,str]:
        cookies=SimpleCookie()
        try:
            cookies.load(self.headers.get("Cookie",""))
            sid=cookies["asc_sid"].value if "asc_sid" in cookies else ""
        except Exception:
            sid=""
        owner=self.hub.session_owner(sid)
        if owner is None and required:
            raise Failure(401,"Sign in to ASC Remote Hub")
        return owner,sid

    def cookie_header(self, token: str, clear: bool=False) -> str:
        attr="; Path=/; SameSite=Lax; HttpOnly"
        if not self.hub.test_mode:
            attr+="; Secure"
        return ("asc_sid=; Max-Age=0" if clear else "asc_sid="+token+"; Max-Age=1209600")+attr

    def csrf_owner(self) -> dict:
        owner,_=self.session()
        csrf=self.headers.get("X-ASC-CSRF", "")
        if not csrf or not hmac.compare_digest(csrf,owner["csrf_token"]):
            raise Failure(403,"CSRF validation failed")
        # Cookie-authenticated write operations can only come from same origin.
        origin=self.headers.get("Origin","")
        if origin and origin != self.hub.base:
            raise Failure(403,"Invalid origin")
        return owner

    def agent_owner(self) -> dict:
        did=self.headers.get("X-ASC-Device", "")
        return self.hub.agent_identity(did,self.bearer())

    def oauth_owner(self) -> dict | None:
        owner=self.hub.oauth_owner(self.bearer())
        if owner is None:
            self.send(401,{"error":"authentication_required"},
                      {"WWW-Authenticate":'Bearer resource_metadata="'+self.hub.base+'/.well-known/oauth-protected-resource", scope="remote.access"'})
        return owner

    def _handler_get(self):
        path=self.route
        if path=="/healthz":
            return self.send(200,{"ok":True,"version":VERSION})
        if path in ("/.well-known/oauth-protected-resource","/.well-known/oauth-protected-resource/mcp"):
            return self.send(200,self.hub.resource_metadata())
        if path in ("/.well-known/oauth-authorization-server","/.well-known/openid-configuration"):
            return self.send(200,self.hub.oauth_metadata())
        if path=="/oauth/authorize":
            fields={k:v[0] for k,v in parse_qs(urlparse(self.path).query,keep_blank_values=True).items()}
            consent_page=self.hub.authorize(fields)
            callback=urlparse(fields["redirect_uri"])
            # Auth validation was successful, so this origin is an exact
            # registered, approved redirect URI. Only permit this callback
            # in the consent page's form-action CSP.
            self._oauth_callback_origin=callback.scheme+"://"+callback.netloc
            return self.send(200,consent_page,mimetype="text/html")
        if path=="/":
            # Choose the HTML document BEFORE sending any response bytes.
            # Authenticated clients receive no login/register form markup,
            # and guests never receive the private dashboard skeleton.
            owner,_=self.session(False)
            view="dashboard.html" if owner is not None else "login.html"
            return self.send(200,(Path(__file__).parent/"static"/view).read_text(encoding="utf-8"),
                             headers={"Vary":"Cookie"},mimetype="text/html")
        # Only distribution names generated from vetted sources are served.
        if path in ("/cli.tgz", "/" + CLI, "/downloads/" + AGENT):
            name = CLI if path=="/cli.tgz" else path.rsplit("/",1)[-1]
            return self.send(200,self.hub.public_packages[name].read_bytes(),
                             mimetype="application/gzip")
        # Platform bootstrap scripts contain no credentials and have an
        # allowlisted origin substituted at request time. They do NOT
        # embed npm, Python or other third-party installer binaries.
        if path in ("/bootstrap.ps1", "/bootstrap.sh"):
            origin=self.hub.base
            if not re.fullmatch(r"https?://(?:[A-Za-z0-9.-]+|\[[0-9a-fA-F:]+\])(?::[0-9]{1,5})?",origin):
                raise Failure(500,"Invalid bootstrap origin")
            asset="bootstrap.ps1" if path.endswith(".ps1") else "bootstrap.sh"
            template=(Path(__file__).parent/"static"/asset).read_text(encoding="utf-8")
            if template.count("'__HUB_ORIGIN__'") != 1:
                raise Failure(500,"Invalid bootstrap template")
            script=template.replace("'__HUB_ORIGIN__'",json.dumps(origin))
            return self.send(200,script,mimetype="text/plain")
        # Serve only explicit, immutable branding assets.
        if path=="/favicon.ico":
            return self.send(200,(Path(__file__).parent/"static/favicon.ico").read_bytes(),
                             mimetype="image/x-icon")
        if path in ("/static/brand-mark.png","/apple-touch-icon.png"):
            return self.send(200,(Path(__file__).parent/"static/brand-mark.png").read_bytes(),
                             mimetype="image/png")
        if path=="/static/style.css":
            return self.send(200,(Path(__file__).parent/"static/style.css").read_text(encoding="utf-8"),mimetype="text/css")
        if path in ("/static/common.js","/static/auth.js","/static/dashboard.js"):
            # Separate scripts keep login-page logic out of the account view.
            return self.send(200,(Path(__file__).parent/"static"/path.rsplit("/",1)[-1]).read_text(encoding="utf-8"),
                             mimetype="application/javascript")
        if path=="/api/me":
            owner,sid=self.session(False)
            if not owner:
                return self.send(200,{"authenticated":False})
            return self.send(200,{"authenticated":True,"email":owner["email"],
                                  "is_admin":bool(owner["is_admin"]),"diagnostic":bool(owner["diagnostic"]),
                                  "csrf":self.hub._session_csrf(sid)})
        if path=="/api/devices":
            owner,_=self.session()
            return self.send(200,{"devices":self.hub.list_devices(owner["id"])})
        if path=="/api/performance":
            owner,_=self.session()
            fields=parse_qs(urlparse(self.path).query)
            try:
                hours=int(fields.get("hours",["24"])[0])
            except (ValueError,TypeError):
                raise Failure(400,"Invalid hours")
            device=fields.get("device",[""])[0]
            return self.send(200,self.hub.performance_summary(
                owner["id"],hours=hours,device=device))
        if path=="/api/oauth/connections":
            owner,_=self.session()
            return self.send(200,self.hub.oauth_connections(owner["id"]))
        if path=="/api/usage":
            owner,_=self.session()
            return self.send(200,self.hub.usage(owner["id"]))
        if path=="/api/errors":
            owner,_=self.session()
            fields=parse_qs(urlparse(self.path).query)
            def item(key,default=""):
                return fields.get(key,[default])[0]
            try:
                days,limit,offset=(int(item("days","30")),
                                   int(item("limit","25")),int(item("offset","0")))
            except (TypeError,ValueError):
                raise Failure(400,"Invalid error pagination parameters")
            return self.send(200,self.hub.errors(
                owner["id"],device=item("device"),tool=item("tool"),
                days=days,limit=limit,offset=offset))
        if path.startswith("/api/errors/") and path.endswith("/trace"):
            owner,_=self.session()
            event_id=path[len("/api/errors/"):-len("/trace")].strip("/")
            return self.send(200,self.hub.error_trace(owner["id"],event_id))
        if path=="/api/diagnostics":
            owner,_=self.session()
            return self.send(200,{"events":self.hub.rows("""SELECT created,device_id,tool,status FROM diagnostics
                                                                WHERE user_id=? ORDER BY created DESC LIMIT 100""",(owner["id"],))})
        if path=="/api/admin/summary":
            owner,_=self.session()
            if not owner["is_admin"]:
                raise Failure(403,"Admin role required")
            return self.send(200,self.hub.admin_summary())
        if path=="/mcp":
            if self.oauth_owner() is None:
                return
            return self.send(405,{"error":"Stateless MCP supports POST only"},{"Allow":"POST, DELETE"})
        raise Failure(404,"Not found")

    def _handler_post(self):
        path=self.route
        if path=="/oauth/register":
            self.hub.throttle_ip("oauth_register",self.client_ip,30,3600)
            return self.send(201,self.hub.oauth_register(self.jsonbody(MAX_FORM)))
        if path=="/oauth/approve":
            location=self.hub.oauth_approve(self.formbody(),self.client_ip)
            return self.send(302,"",{"Location":location},mimetype="text/plain")
        if path=="/oauth/token":
            return self.send(200,self.hub.oauth_token(self.formbody()))
        if path=="/api/register":
            self.hub.throttle_ip("register",self.client_ip,5,3600)
            body=self.jsonbody(4096)
            user=self.hub.register_user(body.get("email",""),body.get("password",""))
            sid,csrf=self.hub.session_create(user["id"])
            return self.send(201,{"ok":True,"email":user["email"],"csrf":csrf},
                             {"Set-Cookie":self.cookie_header(sid)})
        if path=="/api/login":
            self.hub.throttle_ip("login",self.client_ip,24,300)
            body=self.jsonbody(4096)
            user=self.hub.authenticate(body.get("email",""),body.get("password",""))
            if user is None:
                raise Failure(401,"Invalid email or password")
            sid,csrf=self.hub.session_create(user["id"])
            return self.send(200,{"ok":True,"email":user["email"],"csrf":csrf},
                             {"Set-Cookie":self.cookie_header(sid)})
        if path=="/api/oauth/connections/revoke":
            owner=self.csrf_owner()
            body=self.jsonbody(4096)
            if not self.hub.oauth_revoke_connection(owner["id"],body.get("client_id","")):
                raise Failure(404,"OAuth connection not found")
            return self.send(200,{"revoked":True})
        if path=="/api/logout":
            _,sid=self.session()
            self.csrf_owner()
            self.hub.execute("DELETE FROM sessions WHERE hash=?",(digest(sid),))
            return self.send(200,{"ok":True},{"Set-Cookie":self.cookie_header("",True)})
        if path=="/api/devices/pair":
            owner=self.csrf_owner()
            self.hub.throttle_ip("pair",self.client_ip,20,3600)
            code=self.hub.pairing_create(owner["id"])
            return self.send(201,{"pairing_code":code,"expires_in":600})
        if path=="/api/devices/revoke":
            owner=self.csrf_owner()
            body=self.jsonbody(4096)
            did=body.get("device_id","")
            if not isinstance(did,str) or not self.hub.user_device(owner["id"],did):
                raise Failure(404,"Device not found")
            return self.send(200,self.hub.device_revoke(self.hub.user_device(owner["id"],did)))
        if path=="/api/account/diagnostics":
            owner=self.csrf_owner()
            body=self.jsonbody(4096)
            if not isinstance(body.get("enabled"),bool):
                raise Failure(400,"enabled must be boolean")
            self.hub.execute("UPDATE users SET diagnostic=? WHERE id=?",(int(body["enabled"]),owner["id"]))
            return self.send(200,{"diagnostic":body["enabled"],"retention_days":7})
        if path=="/agent/enroll":
            self.hub.throttle_ip("enroll",self.client_ip,40,300)
            body=self.jsonbody(8192)
            return self.send(201,self.hub.pairing_redeem(body.get("pairing_code",""),
                               body.get("name",""),body.get("platform",""),body.get("tools",[])))
        if path=="/agent/unpair":
            # Only this device's bearer token may revoke this exact registration.
            # No cookie or cross-account device identifier is sufficient.
            device=self.agent_owner()
            body=self.jsonbody(512)
            if body.get("confirm_device_id") != device["id"]:
                raise Failure(400,"Device confirmation mismatch")
            return self.send(200,self.hub.device_revoke(device))
        if path in ("/agent/hello","/agent/next","/agent/result"):
            device=self.agent_owner()
            body=self.jsonbody()
            self.trace_begin(device["user_id"], device["id"], body)
            if path=="/agent/hello":
                return self.send(200,self.hub.agent_hello(device,body))
            if path=="/agent/next":
                return self.send(200,self.hub.agent_poll(device))
            return self.send(200,self.hub.agent_result(device,body))
        if path=="/mcp":
            owner=self.oauth_owner()
            if owner is None:
                return
            request=self.jsonbody()
            self.trace_begin(owner["id"], None, request)
            if request.get("jsonrpc")!="2.0":
                raise Failure(400,"Expected JSON-RPC 2.0")
            context=getattr(self,"_trace_ctx",None)
            trace_id=context["request_id"] if context else uuid.uuid4().hex
            resp=self.hub.mcp(owner,request,request_id=trace_id)
            return self.send(200,resp) if resp is not None else self.send(202)
        raise Failure(404,"Not found")

    def do_GET(self):
        try:
            self.require_host()
            self._handler_get()
        except Failure as e:
            self.send(e.status,{"error":e.message})
        except Exception as e:
            print("error",type(e).__name__,flush=True)
            self.send(500,{"error":"Internal error"})

    def do_POST(self):
        try:
            self.require_host()
            self._handler_post()
        except Failure as e:
            self.send(e.status,{"error":e.message})
        except Exception as e:
            print("error",type(e).__name__,flush=True)
            self.send(500,{"error":"Internal error"})

    def do_DELETE(self):
        try:
            self.require_host()
            if self.route=="/mcp":
                if self.oauth_owner() is None:
                    return
                self.send(405,{"error":"No MCP session (stateless)"},{"Allow":"POST"})
            else:
                raise Failure(404,"Not found")
        except Failure as e:
            self.send(e.status,{"error":e.message})

    def do_OPTIONS(self):
        try:
            self.require_host()
            if self.route not in CORS_PUBLIC_PATHS:
                raise Failure(405, "CORS unavailable on cookie/session and agent endpoints")
            wanted=self.headers.get("Access-Control-Request-Method","").upper()
            permitted="POST" if self.route in CORS_POST_PATHS else "GET"
            if wanted != permitted:
                raise Failure(405, "CORS preflight method not allowed")
            requested={v.strip().lower() for v in self.headers.get("Access-Control-Request-Headers","").split(",") if v.strip()}
            if not requested.issubset(CORS_ALLOWED_HEADERS):
                raise Failure(403, "CORS preflight headers not allowed")
            return self.send(204,headers={
                "Access-Control-Allow-Methods":permitted+", OPTIONS",
                "Access-Control-Allow-Headers":", ".join(sorted(CORS_ALLOWED_HEADERS)),
                "Access-Control-Max-Age":"600",
            })
        except Failure as e:
            return self.send(e.status,{"error":e.message})


def start(hub: Hub, host: str, port: int):
    server=ThreadingHTTPServer((host,port),Handler)
    server.daemon_threads=True
    server.hub=hub
    def maintenance():
        while True:
            try:hub.cleanup_expired()
            except Exception as exc:print("maintenance",type(exc).__name__,flush=True)
            time.sleep(3600)
    threading.Thread(target=maintenance,daemon=True,name="asc-hub-maintenance").start()
    print(f"ASC Remote Hub {VERSION} listening on {host}:{port}; public URL {hub.base}",flush=True)
    try:
        server.serve_forever(poll_interval=0.3)
    finally:
        server.server_close()


def cli():
    ap=argparse.ArgumentParser(description="ASC Remote Hub - open registration and user-isolated MCP devices")
    ap.add_argument("command",nargs="?",choices=["serve","create-admin"],default="serve")
    ap.add_argument("--email")
    args=ap.parse_args()
    hub=Hub(base_url=os.environ.get("ASC_HUB_URL",""),data_dir=os.environ.get("ASC_HUB_DATA","/data"),
            test_mode=os.environ.get("ASC_TEST_MODE")=="1")
    if args.command=="create-admin":
        email=args.email or input("Admin email: ")
        password=getpass.getpass("New admin password (at least 12 characters): ")
        try:
            print("Admin created:",hub.create_admin(email,password)["email"])
        except Failure as e:
            print("Error:",e.message)
            raise SystemExit(1)
    else:
        start(hub,os.environ.get("ASC_HUB_LISTEN","0.0.0.0"),int(os.environ.get("ASC_HUB_PORT","8765")))

if __name__=="__main__":
    cli()
