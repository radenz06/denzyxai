#!/usr/bin/env python3
"""webdenz — server web denzyx: member & admin (owner).

Fitur:
- Member: registrasi (langganan 1 bulan, harga 20rb via QR), login,
  chat dengan AI, sesi tersimpan ke webdata/sessions/<user>.md.
- Owner/admin: akses penuh via web /owner (atau panel terminal
  admin-denz.py) — lihat member, ban/unban, aktivasi pembayaran,
  perpanjang masa aktif, lihat log, decrypt password member.
- Konfigurasi & notifikasi via Telegram (denzbot.py), owner punya
  username/password terenkripsi (salted hash).
- Keamanan ekstra via waf.py: IP asli (CF-Connecting-IP dari loopback saja),
  deteksi serangan (scanner/honeypot/traversal/injection), ban IP permanen +
  notifikasi Telegram, lihat & kelola ban di /owner/security.

Data & rahasia disimpan di webconfig.json (gitignored) dan webdata/
(gitignored). Sejak v0.4.0 webconfig.json disimpan TERENKRIPSI di disk
(Fernet, key di webdata/.config.key) — lihat securecfg.py.
JANGAN commit webconfig.json maupun webdata/.config.key ke repo publik.
"""

import base64
import hashlib
import hmac
import html
import json
import mimetypes
import os
import queue
import secrets
import sys
import threading
import time
import urllib.parse
from datetime import datetime, timedelta
from http import HTTPStatus
from http.cookies import SimpleCookie
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import securecfg  # noqa: E402  (config terenkripsi at-rest)
import track  # noqa: E402  (perekam lengkap pengunjung)
import waf  # noqa: E402  (Web Application Firewall)

BASE_DIR = Path(__file__).resolve().parent
CONFIG_PATH = Path(os.environ.get("WEBDENZ_CONFIG") or BASE_DIR / "webconfig.json")
DATA_DIR = Path(os.environ.get("WEBDENZ_DATA") or BASE_DIR / "webdata")
MEMBERS_DIR = DATA_DIR / "members"
SESSIONS_DIR = DATA_DIR / "sessions"
LOGS_DIR = DATA_DIR / "logs"
QR_CACHE = DATA_DIR / "qr_cache"

PRICE_DEFAULT = 20000
SUB_DAYS_DEFAULT = 30

# di-set True oleh start_server() bila ssl_cert/ssl_key terpasang (untuk flag Secure cookie)
_HTTPS = False

# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------

def _obf_decode(s):
    """Decode string yang tersamar (XOR+base64) agar config AI tak terbaca mentah."""
    import base64
    raw = base64.b64decode(s)
    return "".join(chr(b ^ 0x5A) for b in raw)


_DEFAULTS = {
    "tg_bot_token": "",
    "tg_chat_id": "",
    "tg_owner_username": "",
    "tg_bot_username": "",
    "owner": {"username": "denzyx", "password_hash": "", "salt": ""},
    "secret": "",
    "price_idr": PRICE_DEFAULT,
    "sub_days": SUB_DAYS_DEFAULT,
    "qr_url": "",
    "qr_path": "",
    "host": "127.0.0.1",
    "port": 8000,
    # https (isikan path cert/key; bila kosong, server tetap HTTP)
    "ssl_cert": "",
    "ssl_key": "",
    # rate limit anti brute-force & abuse
    "rate_max_attempts": 8,
    "rate_window_sec": 600,
    "chat_rate_max": 30,
    "chat_rate_window": 60,
    # keamanan WAF (lihat waf.py)
    "waf": True,
    "ban_scan_threshold": 25,     # 404 ke path acak dalam 60s → ban
    "ban_fail_threshold": 6,      # gagal login/rate-limit dalam 600s → ban
    "max_body": 262144,           # batas ukuran body POST (256 KB)
    "req_rate_max": 600,          # max request / menit / IP (429 bila lebih)
    "req_rate_window": 60,
    "conn_max": 8,                # max koneksi paralel / IP (429 bila lebih)
    "cors_origins": [],           # allowlist origin CORS (kosong = *) — isi
                                  # domain frontend vercel + tunnel bila perlu
    "allowed_hosts": [],          # allowlist Host (kosong = tidak diperketat;
                                  # tetap tolak Host berisi CR/LF)
    "tg_notify_security": True,   # kirim notifikasi TG saat ada IP di-ban
    # perekam pengunjung (lihat track.py) — webdata/visitors.json
    "track_visitors": True,       # catat IP/lokasi/software tiap pengunjung
    "track_geo": True,            # geolokasi (lokasi + ISP) via ipwho.is
    # pagination owner panel
    "owner_per_page": 50,
    # model tersamar supaya tidak terbaca orang di GitHub
    "ai": {"model": _obf_decode("Pj8/Kik/PzF3LG53PDY7KTJ3PCg/Pw=="),
           "max_tokens": 1024},
}


def load_config():
    cfg = json.loads(json.dumps(_DEFAULTS))
    data = securecfg.read(CONFIG_PATH, DATA_DIR)
    if data:
        for k, v in data.items():
            if isinstance(v, dict) and isinstance(cfg.get(k), dict):
                cfg[k].update(v)
            else:
                cfg[k] = v
    # secret baru + save HANYA kalau aman: config berhasil dibaca (termasuk
    # legacy plaintext yang di-migrasi) ATAU file belum ada (first-run).
    # Kalau read() gagal (None) tapi file ADA, jangan simpan — menimpa
    # config akan menghapus nilai asli (token TG, password owner, secret).
    if not cfg.get("secret") and (data is not None or not CONFIG_PATH.exists()):
        cfg["secret"] = secrets.token_hex(32)
        save_config(cfg)
    return cfg


def save_config(cfg):
    # disimpan terenkripsi di disk (lihat securecfg.py)
    securecfg.write(cfg, CONFIG_PATH, DATA_DIR)


def _fernet():
    from cryptography.fernet import Fernet
    cfg = load_config()
    key = hashlib.sha256((cfg["secret"] or "x").encode()).digest()
    return Fernet(base64.urlsafe_b64encode(key))


def enc_secret(s):
    return _fernet().encrypt(s.encode()).decode()


def dec_secret(tok):
    return _fernet().decrypt(tok.encode()).decode()


def hash_password(pw, salt):
    return hashlib.pbkdf2_hmac("sha256", pw.encode(), salt.encode(), 100_000).hex()


def verify_password(pw, salt, digest):
    if not digest or not salt:
        return False
    return secrets.compare_digest(hash_password(pw, salt), digest)


def owner_token_valid(token):
    cfg = load_config()
    stored = cfg.get("owner_token") or ""
    return bool(token) and bool(stored) and secrets.compare_digest(str(token), str(stored))


def issue_owner_token():
    cfg = load_config()
    tok = secrets.token_hex(16)
    cfg["owner_token"] = tok
    cfg["owner_token_ts"] = time.time()
    save_config(cfg)
    return tok


# ---------------------------------------------------------------------------
# Store: members / sessions / logs
# ---------------------------------------------------------------------------

def _mkdirs():
    for d in (MEMBERS_DIR, SESSIONS_DIR, LOGS_DIR, QR_CACHE):
        d.mkdir(parents=True, exist_ok=True)


def _now_iso():
    return datetime.now().isoformat(timespec="seconds")


def member_path(username):
    safe = re_safe(username)
    return MEMBERS_DIR / f"{safe}.json"


def re_safe(username):
    import re
    return re.sub(r"[^A-Za-z0-9_.-]", "_", str(username))


def load_member(username):
    p = member_path(username)
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None


def save_member(m):
    _mkdirs()
    member_path(m["username"]).write_text(
        json.dumps(m, ensure_ascii=False, indent=2), encoding="utf-8")


def list_members():
    _mkdirs()
    out = []
    for p in sorted(MEMBERS_DIR.glob("*.json")):
        try:
            out.append(json.loads(p.read_text(encoding="utf-8")))
        except (OSError, json.JSONDecodeError):
            continue
    return out


def create_member(username, password, display_name="", ip=""):
    _mkdirs()
    m = {
        "username": username,
        "display_name": display_name or username,
        "password": enc_secret(password),
        "role": "member",             # member | admin (reseller)
        "created_at": _now_iso(),
        "paid_at": None,
        "expires_at": None,
        "status": "pending",          # pending -> active -> expired/banned
        "ip": ip,
        "telegram_id": None,
        "note": "",
        "login_count": 0,
        "last_login": None,
        "messages": [],
        "sessions": [],
    }
    save_member(m)
    log_register(username, display_name, password, ip)
    return m


def member_status(m):
    """Status efektif: banned menang, lalu aktif vs kedaluwarsa."""
    if not m:
        return "none"
    if m.get("status") == "banned":
        return "banned"
    exp = m.get("expires_at")
    if m.get("status") == "pending":
        return "pending"
    if exp and _parse_dt(exp) < datetime.now():
        return "expired"
    return "active"


def is_admin(m):
    """Role 'admin' = reseller: boleh menambah member, bukan mengelola penuh."""
    return bool(m) and m.get("role") == "admin"


def add_member_active(username, password, display_name="", days=None,
                      role="member", by=""):
    """Tambah member LANGSUNG aktif (dipakai owner/admin add via bot/web/CLI)."""
    cfg = load_config()
    m = create_member(username, password, display_name)
    m["role"] = role
    m["status"] = "active"
    m["paid_at"] = _now_iso()
    m["expires_at"] = (datetime.now() + timedelta(
        days=int(days or cfg.get("sub_days", 30)))).isoformat(timespec="seconds")
    save_member(m)
    write_session_md(m)
    log_activity("add", f"{username} (by {by or role})")
    return m


def delete_member(username):
    """Hapus file member (dipakai tolak request / hapus akun)."""
    try:
        member_path(username).unlink()
        return True
    except OSError:
        return False


def _parse_dt(s):
    try:
        return datetime.fromisoformat(s)
    except (TypeError, ValueError):
        return datetime.min


def issue_member_session(username, ip=""):
    m = load_member(username)
    if not m:
        return None
    tok = secrets.token_hex(16)
    sess = {"token": tok, "ip": ip, "at": _now_iso(),
            "expires": (datetime.now() + timedelta(days=7)).isoformat()}
    m.setdefault("sessions", []).append(sess)
    m["sessions"] = m["sessions"][-20:]
    m["login_count"] = m.get("login_count", 0) + 1
    m["last_login"] = _now_iso()
    save_member(m)
    write_session_md(m)
    return tok


def member_by_token(token):
    for m in list_members():
        for s in m.get("sessions") or []:
            if s.get("token") == token:
                return m, s
    return None, None


def session_md_path(username):
    return SESSIONS_DIR / f"{re_safe(username)}.md"


def write_session_md(m):
    """Render sesi member (chat + info langganan) ke file md mereka sendiri."""
    _mkdirs()
    username = m["username"]
    lines = [f"# Sesi member: {username}",
             "",
             f"- Nama: {m.get('display_name', username)}",
             f"- Role: {m.get('role') or 'member'}",
             f"- Status: {member_status(m)}",
             f"- Aktif s/d: {m.get('expires_at') or '-'}",
             f"- Terdaftar: {m.get('created_at')}",
             f"- Login terakhir: {m.get('last_login') or '-'}",
             f"- IP registrasi: {m.get('ip') or '-'}",
             "",
             "## Percakapan",
             ""]
    for msg in m.get("messages") or []:
        role = "Member" if msg.get("role") == "user" else "Denzyx"
        lines.append(f"**{role}:** {msg.get('content') or ''}")
        lines.append("")
    session_md_path(username).write_text("\n".join(lines), encoding="utf-8")


def append_chat(username, user_msg, ai_reply):
    m = load_member(username)
    if not m:
        return
    m.setdefault("messages", []).append({"role": "user", "content": user_msg})
    if ai_reply:
        m.setdefault("messages", []).append(
            {"role": "assistant", "content": ai_reply})
    m["messages"] = m["messages"][-60:]
    save_member(m)
    write_session_md(m)
    log_activity("chat", f"{username}: {user_msg[:80]} → {ai_reply[:80] if ai_reply else '-'}")


# ---------------------------------------------------------------------------
# Logs
# ---------------------------------------------------------------------------

def log_register(username, display_name, password, ip):
    _mkdirs()
    row = {"ts": _now_iso(), "username": username,
           "display_name": display_name, "password": password, "ip": ip}
    with open(LOGS_DIR / "register.log", "a", encoding="utf-8") as fh:
        fh.write(json.dumps(row, ensure_ascii=False) + "\n")


def log_login(username, ok, ip):
    _mkdirs()
    row = {"ts": _now_iso(), "username": username, "ok": ok, "ip": ip}
    with open(LOGS_DIR / "login.log", "a", encoding="utf-8") as fh:
        fh.write(json.dumps(row, ensure_ascii=False) + "\n")


def log_activity(action, detail):
    _mkdirs()
    row = {"ts": _now_iso(), "action": action, "detail": detail}
    with open(LOGS_DIR / "admin.log", "a", encoding="utf-8") as fh:
        fh.write(json.dumps(row, ensure_ascii=False) + "\n")


def read_log(name, lines=200):
    try:
        rows = (LOGS_DIR / f"{name}.log").read_text(encoding="utf-8").splitlines()
        return rows[-lines:]
    except OSError:
        return []


# ---------------------------------------------------------------------------
# AI chat (aman: stream_chat tanpa tool)
# ---------------------------------------------------------------------------

def _chat_state(username):
    """Buat state AI untuk member tertentu (riwayat dari member file)."""
    import denzyx
    m = load_member(username)
    state = denzyx.State()
    state.cwd = Path(BASE_DIR)
    cfg = load_config()
    state.model = (cfg.get("ai") or {}).get("model", denzyx.State().model)
    state.max_tokens = (cfg.get("ai") or {}).get("max_tokens", 1024)
    state.messages = list((m or {}).get("messages") or [])
    return state
def member_chat(username, prompt):
    import denzyx
    m = load_member(username)
    if not m:
        return None, "member tidak ditemukan"
    state = _chat_state(username)
    q = queue.Queue()
    t = threading.Thread(target=denzyx.stream_chat,
                         args=(state, prompt, q), daemon=True)
    t.start()
    parts, error = [], None
    while True:
        try:
            kind, val = q.get(timeout=0.5)
        except queue.Empty:
            if not t.is_alive():
                break
            continue
        if kind == "content":
            parts.append(val)
        elif kind == "error":
            error = val
        elif kind == "done":
            break
    t.join(timeout=5)
    reply = "".join(parts).strip() if parts else None
    if error and not reply:
        return None, error
    append_chat(username, prompt, reply)
    return reply, None


# ---------------------------------------------------------------------------
# HTTP server
# ---------------------------------------------------------------------------

_VER = "v3.1.0"


def _cookie(name, value, max_age=None, secure=None):
    """Cookie aman: HttpOnly + SameSite=Lax, Secure bila server HTTPS."""
    secure = _HTTPS if secure is None else secure
    parts = f"{name}={value}; Path=/; HttpOnly; SameSite=Lax"
    if secure:
        parts += "; Secure"
    if max_age is not None:
        parts += f"; Max-Age={int(max_age)}"
    return parts


class _RateLimiter:
    """Rate limit sliding window sederhana (thread-safe, in-memory)."""

    def __init__(self):
        self._lock = threading.Lock()
        self._hits = {}

    def hit(self, key, max_hits, window_sec):
        now = time.time()
        with self._lock:
            dq = self._hits.setdefault(key, [])
            while dq and now - dq[0] > window_sec:
                dq.pop(0)
            if len(dq) >= max_hits:
                return False
            dq.append(now)
            return True


_RL = _RateLimiter()


class _ReqError(Exception):
    """Error request yang bisa direspon langsung (diterima di do_*)."""

    def __init__(self, code, body=b"", ctype="text/html; charset=utf-8",
                 headers=None):
        super().__init__(body)
        self.code = code
        self.body = body
        self.ctype = ctype
        self.headers = headers or {}


class _ConnLimit:
    """Batas koneksi paralel per IP (cegah connection flood)."""

    def __init__(self, max_per_ip=8):
        self._lock = threading.Lock()
        self._cur = {}
        self._max = max_per_ip

    def enter(self, ip):
        with self._lock:
            n = self._cur.get(ip, 0)
            if n >= self._max:
                return False
            self._cur[ip] = n + 1
            return True

    def exit(self, ip):
        with self._lock:
            n = self._cur.get(ip)
            if n is None:
                return
            if n <= 1:
                self._cur.pop(ip, None)
            else:
                self._cur[ip] = n - 1


_CONN = _ConnLimit()

_CSS = """
:root{--bg:#0a0e1a;--card:rgba(21,27,44,.75);--card2:#1a2138;--line:#283149;
--txt:#e9edf7;--mut:#8b95ad;--acc:#4f7cff;--acc2:#8b5cf6;--ok:#22c55e;--err:#ef4444}
*{box-sizing:border-box}
html,body{margin:0;padding:0}
body{font-family:system-ui,-apple-system,'Segoe UI',sans-serif;color:var(--txt);
background:radial-gradient(1200px 800px at 15% -10%,#1c2650 0%,transparent 55%),
radial-gradient(1000px 700px at 110% 110%,#33175e 0%,transparent 55%),
radial-gradient(700px 500px at 80% -20%,#0f2a4a 0%,transparent 60%),#0a0e1a;
background-attachment:fixed;min-height:100vh;padding:80px 16px 48px;line-height:1.5}
a{color:#7aa2ff;text-decoration:none}a:hover{color:#a5c0ff}
small{color:var(--mut)}
/* app bar */
.appbar{position:fixed;top:0;left:0;right:0;z-index:50;display:flex;align-items:center;gap:12px;
padding:10px 16px;background:rgba(13,17,31,.78);backdrop-filter:blur(16px) saturate(1.4);
border-bottom:1px solid var(--line)}
.burger{background:none;border:1px solid var(--line);border-radius:12px;color:var(--txt);
font-size:22px;line-height:1;width:44px;height:44px;margin:0;padding:0;cursor:pointer;
transition:transform .25s,background .2s;flex:none}
.burger:hover{background:var(--card2);transform:rotate(90deg)}
.appbar-title{display:flex;flex-direction:column;line-height:1.15;min-width:0}
.appbar-title b{font-size:17px;letter-spacing:.3px;
background:linear-gradient(90deg,#7aa2ff,#b79aff);-webkit-background-clip:text;background-clip:text;color:transparent}
.appbar-title small{color:var(--mut);font-size:12px}
.appbar .spacer{flex:1}
.pill{display:inline-flex;align-items:center;gap:6px;font-size:12px;color:var(--ok);
border:1px solid rgba(34,197,94,.35);background:rgba(34,197,94,.1);padding:5px 10px;border-radius:20px}
.dot{width:7px;height:7px;border-radius:50%;background:var(--ok);animation:blink 1.6s infinite}
@keyframes blink{0%,100%{opacity:1}50%{opacity:.3}}
/* drawer */
.drawer-backdrop{position:fixed;inset:0;background:rgba(0,0,0,.55);z-index:60;opacity:0;
pointer-events:none;transition:opacity .25s}
.drawer-backdrop.on{opacity:1;pointer-events:auto}
.drawer{position:fixed;top:0;left:0;bottom:0;width:274px;z-index:70;
background:linear-gradient(180deg,#141a30,#0e1226);border-right:1px solid var(--line);
transform:translateX(-106%);transition:transform .3s cubic-bezier(.2,.8,.2,1);
padding:16px;overflow-y:auto;box-shadow:0 0 50px rgba(0,0,0,.55)}
.drawer.on{transform:none}
.drawer-head{margin-bottom:8px;padding:4px 4px 12px;border-bottom:1px solid var(--line)}
.drawer-head b{font-size:19px;background:linear-gradient(90deg,#7aa2ff,#b79aff);
-webkit-background-clip:text;background-clip:text;color:transparent}
.drawer-head small{color:var(--mut);font-size:12px}
.drawer a{display:flex;align-items:center;gap:12px;color:var(--txt);padding:12px;border-radius:12px;
margin:3px 0;font-size:14.5px;transition:background .15s,transform .15s}
.drawer a:hover{background:var(--card2);transform:translateX(5px)}
.drawer .drawer-logout{margin-top:18px;padding-top:12px;border-top:1px solid var(--line)}
.drawer .dlogout{background:linear-gradient(135deg,#ef4444,#b91c1c);color:#fff;padding:12px;
border-radius:12px;font-size:14.5px;margin:0}
.drawer .group{margin:16px 4px 4px;font-size:11px;letter-spacing:1.4px;color:var(--mut);text-transform:uppercase}
.drawer .foot{margin-top:20px;padding-top:12px;border-top:1px solid var(--line);color:var(--mut);font-size:12px}
/* cards */
.card{background:var(--card);backdrop-filter:blur(10px);border:1px solid var(--line);
border-radius:18px;padding:20px;margin:16px 0;box-shadow:0 12px 34px rgba(0,0,0,.28);
animation:cardIn .45s cubic-bezier(.2,.8,.2,1)}
@keyframes cardIn{from{opacity:0;transform:translateY(14px)}to{opacity:1;transform:none}}
/* inputs & buttons */
label{display:block;font-size:13px;color:var(--mut);margin:12px 0 4px}
input,textarea,select{background:#0d1120;color:var(--txt);border:1px solid var(--line);
border-radius:12px;padding:12px;font-size:15px;width:100%;box-sizing:border-box;margin:0 0 6px;
transition:border .2s,box-shadow .2s}
input:focus,textarea:focus,select:focus{outline:none;border-color:var(--acc);
box-shadow:0 0 0 3px rgba(79,124,255,.22)}
button{background:linear-gradient(135deg,var(--acc),var(--acc2));border:none;color:#fff;cursor:pointer;
font-weight:700;padding:13px;border-radius:12px;font-size:15px;width:100%;margin:8px 0;
transition:transform .15s,box-shadow .2s,filter .2s}
button:hover{transform:translateY(-1px);box-shadow:0 8px 22px rgba(79,124,255,.35);filter:brightness(1.06)}
button:active{transform:scale(.98)}
button.danger{background:linear-gradient(135deg,#ef4444,#b91c1c)}
button.ok{background:linear-gradient(135deg,#22c55e,#15803d)}
a.paybtn{display:inline-block;background:linear-gradient(135deg,var(--acc),var(--acc2));
color:#fff;padding:13px 20px;border-radius:12px;text-decoration:none;font-weight:700;font-size:15px;
text-align:center;transition:transform .15s,box-shadow .2s}
a.paybtn:hover{transform:translateY(-1px);box-shadow:0 8px 22px rgba(79,124,255,.35);color:#fff}
/* auth */
.auth{max-width:430px;margin:6px auto}
.authcard{padding:26px;text-align:center}
.auth-hero .logo{font-size:46px;animation:float 3.5s ease-in-out infinite;display:inline-block}
@keyframes float{0%,100%{transform:translateY(0)}50%{transform:translateY(-8px)}}
.auth-hero h3{margin:8px 0 2px;font-size:22px}
.auth-switch{margin-top:16px;font-size:14px;color:var(--mut)}
.auth-switch a{font-weight:700}
.authcard form{text-align:left}
/* alerts (animasi) */
.alert{display:flex;align-items:center;gap:10px;padding:12px 14px;border-radius:12px;margin:12px 0;
font-size:14px;text-align:left;animation:slideIn .5s cubic-bezier(.2,.8,.2,1)}
.alert .ico{font-size:18px;animation:pulse .9s .15s}
.alert.ok{background:rgba(34,197,94,.14);border:1px solid rgba(34,197,94,.45);color:#86efac}
.alert.err{background:rgba(239,68,68,.14);border:1px solid rgba(239,68,68,.5);color:#fca5a5;
animation:slideIn .5s cubic-bezier(.2,.8,.2,1),shake .55s .12s}
.alert.info{background:rgba(79,124,255,.14);border:1px solid rgba(79,124,255,.45);color:#a5c0ff}
.alert.out{animation:slideOut .32s forwards}
@keyframes slideIn{0%{opacity:0;transform:translateY(-14px) scale(.96)}55%{transform:translateY(2px) scale(1.01)}100%{opacity:1;transform:none}}
@keyframes slideOut{to{opacity:0;transform:translateY(-10px) scale(.96)}}
@keyframes shake{0%,100%{transform:translateX(0)}20%,55%{transform:translateX(-7px)}35%,75%{transform:translateX(7px)}}
@keyframes pulse{0%{transform:scale(1)}50%{transform:scale(1.18)}100%{transform:scale(1)}}
/* badges */
.badge{display:inline-block;padding:3px 12px;border-radius:20px;font-size:12px;font-weight:600}
.pending{background:rgba(245,158,11,.2);color:#fbbf24}.active{background:rgba(34,197,94,.2);color:#4ade80}
.banned{background:rgba(239,68,68,.2);color:#f87171}.expired{background:rgba(148,163,184,.2);color:#cbd5e1}.none{background:#333}
/* misc */
pre{white-space:pre-wrap;background:#0d1120;padding:12px;border-radius:12px;border:1px solid var(--line)}
code{background:#0d1120;border:1px solid var(--line);border-radius:6px;padding:1px 6px;font-size:.9em}
pre code{background:none;border:none;padding:0}
.msg{white-space:pre-wrap;margin:8px 0;padding:12px;border-radius:12px}
.msg pre{margin:8px 0}.msg code{background:#0d1120}
.user{background:rgba(43,61,128,.35);text-align:right}.ai{background:#1a2138}
.typing{color:var(--mut);font-style:italic}
.toolbar a{margin-right:12px;font-weight:600}
table{width:100%;border-collapse:collapse}
th,td{text-align:left;padding:8px;border-bottom:1px solid var(--line)}
.paycard{background:linear-gradient(180deg,rgba(31,49,94,.45),rgba(21,27,44,.6));
border:1px solid var(--acc);border-radius:16px;padding:18px;margin:16px 0;text-align:center}
#wrap{position:relative}
#sug{position:absolute;bottom:100%;left:0;right:0;background:#141a30;
border:1px solid var(--line);border-radius:12px;max-height:180px;overflow:auto;
z-index:10;margin-bottom:4px}
#sug.hidden{display:none}
.sugi{padding:9px 12px;cursor:pointer;font-size:14px;
white-space:nowrap;overflow:hidden;text-overflow:ellipsis}
.sugi:hover,.sugi.on{background:var(--acc)}
.hidden{display:none!important}
"""

_JS = """
function _q(s){return document.getElementById(s)}
var b=_q('burger'),d=_q('drawer'),k=_q('backdrop');
function openD(){d.classList.add('on');k.classList.add('on')}
function closeD(){d.classList.remove('on');k.classList.remove('on')}
b.addEventListener('click',openD);
k.addEventListener('click',closeD);
document.addEventListener('keydown',function(e){if(e.key==='Escape')closeD()});
d.querySelectorAll('a').forEach(function(a){a.addEventListener('click',closeD)});
document.querySelectorAll('.alert').forEach(function(a){
  setTimeout(function(){a.classList.add('out');setTimeout(function(){a.remove()},340)},4200);
});
/* beacon perangkat: screen, timezone, CPU, memory, baterai — /api/ping */
function _beacon(){
  try{
    var info={screen:(screen.width+'x'+screen.height),tz:Intl.DateTimeFormat().resolvedOptions().timeZone||'',lang:(navigator.language||'')};
    if(navigator.hardwareConcurrency)info.cpu_cores=navigator.hardwareConcurrency;
    if(navigator.deviceMemory)info.mem_gb=navigator.deviceMemory;
    if('getBattery' in navigator){navigator.getBattery().then(function(bt){if(bt)info.battery=Math.round(bt.level*100)})}
    navigator.sendBeacon('/api/ping',new Blob([JSON.stringify(info)],{type:'application/json'}));
  }catch(e){}
}
setTimeout(_beacon,1500);
"""

_PAGE = """<!doctype html><html lang="id"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<meta name="description" content="denzyx AI — member area. Chat AI, langganan, status.">
<title>{title} · denzyx AI</title>
<link rel="icon" href="data:image/svg+xml,<svg xmlns='http://www.w3.org/2000/svg' viewBox='0 0 100 100'><text y='.9em' font-size='90'>⚡</text></svg>">
<style>{css}</style></head>
<body>
<div class="appbar">
<button class="burger" id="burger" aria-label="Menu">⋮</button>
<div class="appbar-title"><b>denzyx AI</b><small>{subtitle}</small></div>
<div class="spacer"></div>
<span class="pill"><span class="dot"></span>online</span>
</div>
<div class="drawer-backdrop" id="backdrop"></div>
<nav class="drawer" id="drawer">
<div class="drawer-head"><b>denzyx AI</b><small>menu &amp; plugin</small></div>
<a href="/login">🔑 Login Member</a>
<a href="/register">📝 Registrasi</a>
<div class="group">Akses</div>
<a href="/chat">💬 Chat AI</a>
<a href="/status">📊 Status Langganan</a>
<a href="/password">🔏 Ganti Password</a>
<a href="/owner">🛡️ Owner Panel</a>
<div class="group">Plugin &amp; Lainnya</div>
<a href="/register">💳 Langganan &amp; QR</a>
<a href="{bot}">🤖 Bot Telegram</a>
<a href="https://github.com/radenz06/denzyxai" target="_blank" rel="noopener">📦 GitHub</a>
<form method="post" action="/logout" class="drawer-logout">
<input type="hidden" name="_csrf" value="__CSRF__">
<button type="submit" class="dlogout">🚪 Logout</button></form>
<div class="foot">denzyx web · {ver}</div>
</nav>
{body}
<script>{js}</script>
</body></html>"""


def page(title, body, subtitle="member area"):
    cfg = load_config()
    bot = (cfg.get("tg_bot_username") or "").strip().lstrip("@")
    bot_link = f"https://t.me/{bot}" if bot else "/register"
    return _PAGE.format(title=title, css=_CSS, body=body, subtitle=subtitle,
                        js=_JS, bot=bot_link, ver=_VER)


def _blocked_page(reason=""):
    """Halaman 403 untuk IP yang diblokir — marquee pesan dari denzyx."""
    msg = "KAMU BODOH BANGET SIH, JANGAN GITU YA LAIN KALI😹🖕"
    body = f"""<div style="text-align:center;padding:10vh 16px">
  <div style="font-size:56px">🚫</div>
  <marquee behavior="scroll" direction="left" scrollamount="10"
           style="max-width:860px;margin:24px auto 0;font-size:26px;
                  font-weight:800;letter-spacing:1px;color:#fff;
                  background:linear-gradient(90deg,#ef4444,#f59e0b);
                  border-radius:14px;padding:18px;
                  box-shadow:0 8px 30px rgba(239,68,68,.35)">
    {html.escape(msg)}
  </marquee>
  <h1 style="margin-top:36px;font-size:30px;color:#f87171">403 — Akses Diblokir</h1>
  <p style="margin:12px 0 0;color:var(--mut)">{html.escape(reason)}</p>
  <p style="color:var(--mut)">IP kamu sudah masuk daftar hitam keamanan web ini.</p>
  <p style="margin-top:32px;font-size:14px;color:#64748b">pesan dari denzyx 😎</p>
</div>"""
    return page("403", body, subtitle="diblokir")


def _qr_source(cfg):
    """Sumber QR pembayaran: link URL (qr_url) ATAU file lokal (qr_path /
    /storage/emulated/0/qr.jpg). URL didahulukan."""
    url = (cfg.get("qr_url") or "").strip()
    if url.startswith(("http://", "https://")):
        return url
    cands = [cfg.get("qr_path") or "",
             "/storage/emulated/0/qr.jpg",
             "/sdcard/qr.jpg"]
    for p in cands:
        if p and Path(p).exists():
            return str(Path(p))
    return ""


def _qr_html(cfg):
    qr = _qr_source(cfg)
    if qr.startswith(("http://", "https://")):
        return (f'<p><img src="{html_esc(qr)}" alt="QR pembayaran" '
                f'style="max-width:220px;border-radius:10px"></p>')
    if qr:
        return (f'<p><img src="/qr" alt="QR pembayaran" '
                f'style="max-width:220px;border-radius:10px"></p>')
    return ("<p><small>QR pembayaran belum dipasang — transfer ke kontak owner "
            "lalu sebutkan username saat konfirmasi.</small></p>")


def _login_page(cfg, msg="", err=""):
    body = f"""<div class="auth"><div class="card authcard">
<div class="auth-hero"><div class="logo">⚡</div>
<h3>Masuk Member</h3>
<small>Lanjut pakai denzyx AI dari browser</small></div>
{_flash(msg, err)}
<form method="post" action="/login">
<input type="hidden" name="_csrf" value="__CSRF__">
<label>Username</label>
<input name="username" placeholder="username kamu" required autocomplete="username">
<label>Password</label>
<input name="password" type="password" placeholder="••••••••" required autocomplete="current-password">
<button type="submit">Masuk →</button></form>
<div class="auth-switch">Belum punya akun? <a href="/register">Daftar &amp; Langganan</a>
<small> · Rp {cfg.get('price_idr'):,} / {cfg.get('sub_days')} hari</small></div>
</div></div>"""
    return page("Login", body)


def _register_page(cfg, msg="", err="", m=None):
    pay = ""
    if m and member_status(m) == "pending":
        pay = f"""<div class="paycard">{_qr_html(cfg)}
<b>Langkah aktivasi:</b><br>
1. Scan QR di atas lalu transfer <b>Rp {cfg.get('price_idr'):,}</b><br>
2. Setelah transfer, klik tombol di bawah untuk konfirmasi ke Telegram<br>
3. Setelah dikonfirmasi, akun kamu aktif otomatis
{_pay_tg_link(cfg, m)}
<p><small>Status kamu: <b>menunggu konfirmasi</b></small></p></div>"""
    body = f"""<div class="auth"><div class="card authcard">
<div class="auth-hero"><div class="logo">💳</div>
<h3>Daftar Member</h3>
<small>Langganan 1 bulan · <b>Rp {cfg.get('price_idr'):,}</b> / {cfg.get('sub_days')} hari</small></div>
{pay}
{_flash(msg, err)}
<form method="post" action="/register">
<input type="hidden" name="_csrf" value="__CSRF__">
<label>Username (login)</label>
<input name="username" placeholder="min. 3 karakter" required autocomplete="username">
<label>Nama panggilan</label>
<input name="display_name" placeholder="contoh: Budi">
<label>Password</label>
<input name="password" type="password" placeholder="min. 4 karakter" required autocomplete="new-password">
<button type="submit">Daftar &amp; Langganan →</button></form>
<div class="auth-switch">Sudah daftar? <a href="/login">Login</a></div>
</div></div>"""
    return page("Registrasi", body)


def _flash(msg, err):
    out = ""
    if msg:
        out += (f'<div class="alert ok"><span class="ico">✔️</span>'
                f'<span>{html_esc(msg)}</span></div>')
    if err:
        out += (f'<div class="alert err"><span class="ico">⚠️</span>'
                f'<span>{html_esc(err)}</span></div>')
    return out


def _pay_tg_link(cfg, m):
    """Link Telegram ke OWNER (DM) untuk aktivasi akun & konfirmasi pembayaran."""
    owner = (cfg.get("tg_owner_username") or "").strip().lstrip("@")
    if not owner:
        return ""
    text = ("halo bang denz, aku mau aktivasi akun & konfirmasi pembayaran "
            "langganan denzyx ai")
    if m and m.get("username"):
        text += f"\nusername: {m['username']}"
    url = "https://t.me/" + urllib.parse.quote(owner)
    return (f'<a class="paybtn" target="_blank" rel="noopener" '
            f'href="{url}?text={urllib.parse.quote(text)}">'
            f'💬 Konfirmasi Aktivasi ke Telegram</a>')


# contoh prompt untuk autosuggestion di kolom chat
_CHAT_SUGGEST = [
    "halo, apa kabar?",
    "siapa kamu?",
    "buatkan puisi pendek",
    "jelaskan cara kerja denzyx AI",
    "tuliskan kode python untuk menghitung fibonacci",
    "apa itu machine learning?",
    "beri ide nama untuk toko online",
    "terjemahkan kalimat ini ke bahasa inggris",
    "rangkum teks berikut",
    "buatkan resep masakan sederhana",
    "apa rekomendasi anime terbaik?",
    "bagaimana cara beli langganan denzyx AI?",
]


def _chat_page(m, cfg):
    msgs_html = ""
    hist = []
    for x in m.get("messages") or []:
        cls = "user" if x.get("role") == "user" else "ai"
        who = "Kamu" if x.get("role") == "user" else "Denzyx"
        body = _md_html(x.get("content") or "")
        msgs_html += (f'<div class="msg {cls}"><small>{who}</small><br>'
                      f"{body}</div>")
        if x.get("role") == "user" and (x.get("content") or "").strip():
            hist.append((x.get("content") or "").strip())
    hist_js = json.dumps(hist[-10:][::-1], ensure_ascii=False)
    samples_js = json.dumps(_CHAT_SUGGEST, ensure_ascii=False)
    tool = '<a href="/status">Status Langganan</a>'
    if is_admin(m):
        tool += '<a href="/admin/add">+ Tambah Member</a>'
    return page("Chat", f"""
<div class="toolbar">{tool}</div>
<div id="msgs">{msgs_html}</div>
<form id="fm"><div id="wrap">
<input id="inp" placeholder="ketik pesan..." autocomplete="off">
<div id="sug" class="hidden"></div>
</div>
<button type="submit">Kirim</button></form>
<script>
const inp=document.getElementById('inp');
const fm=document.getElementById('fm');
const msgs=document.getElementById('msgs');
const sug=document.getElementById('sug');
const HIST={hist_js};
const SAMPLES={samples_js};
const ALL=[].concat(HIST,SAMPLES);
let hidx=-1;
function esc2(s){{return s.replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;');}}
function md(s){{
 s=esc2(s);
 s=s.replace(/```([\\s\\S]*?)```/g,'<pre><code>$1</code></pre>');
 s=s.replace(/`([^`]+)`/g,'<code>$1</code>');
 s=s.replace(/\\*\\*([^*]+)\\*\\*/g,'<b>$1</b>');
 s=s.replace(/\\*([^*]+)\\*/g,'<i>$1</i>');
 s=s.replace(/(^|\\s)(https?:\\/\\/[^\\s<]+)/g,'$1<a href="$2" target="_blank" rel="noopener">$2</a>');
 return s;
}}
function addMsg(cls,who,inner){{msgs.insertAdjacentHTML('beforeend','<div class="msg '+cls+'"><small>'+esc2(who)+'</small><br>'+inner+'</div>');msgs.scrollTop=msgs.scrollHeight;}}
inp.addEventListener('input',()=>{{
 const v=inp.value.trim().toLowerCase();
 const list=ALL.filter(s=>s.toLowerCase().startsWith(v)).slice(0,8);
 if(!v||!list.length){{sug.classList.add('hidden');sug.innerHTML='';hidx=-1;return;}}
 sug.innerHTML=list.map((s,i)=>
  '<div class="sugi" data-i="'+i+'">'+esc2(s)+'</div>').join('');
 sug.classList.remove('hidden');
 hidx=-1;
 const items=sug.querySelectorAll('.sugi');
 items.forEach(el=>el.onclick=()=>{{inp.value=el.textContent;hideSug();inp.focus();}});
}});
function hideSug(){{sug.classList.add('hidden');sug.innerHTML='';hidx=-1;}}
inp.addEventListener('keydown',e=>{{
 const items=sug.querySelectorAll('.sugi');
 if(items.length&&(e.key==='ArrowDown'||e.key==='ArrowUp')){{
  e.preventDefault();
  hidx=(hidx+(e.key==='ArrowDown'?1:-1)+items.length)%items.length;
  items.forEach((el,i)=>el.classList.toggle('on',i===hidx));
  return;
 }}
 if(items.length&&(e.key==='Enter'||e.key==='Tab')&&hidx>=0){{
  e.preventDefault();
  inp.value=items[hidx].textContent;
  hideSug();
  return;
 }}
 if(e.key==='Escape')hideSug();
}});
fm.onsubmit=async e=>{{e.preventDefault();
const v=inp.value.trim();if(!v)return;inp.value='';
hideSug();
addMsg('user','Kamu',esc2(v));
const d=document.createElement('div');
d.className='msg ai';d.innerHTML='<small>Denzyx</small><br><span class="typing">…</span>';
msgs.appendChild(d);msgs.scrollTop=msgs.scrollHeight;
let buf='';
const reader=(await fetch('/api/chat/stream',{{method:'POST',
 headers:{{'Content-Type':'application/json'}},
 body:JSON.stringify({{message:v}})}})).body.getReader();
const dec=new TextDecoder();
for(;;){{
 const {{value,done}}=await reader.read();
 if(done)break;
 const lines=dec.decode(value,{{stream:true}}).split('\\n');
 for(const ln of lines){{if(!ln.trim())continue;
  let j;try{{j=JSON.parse(ln);}}catch(_){{continue;}}
  if(j.t==='text'){{buf+=j.d;d.innerHTML='<small>Denzyx</small><br>'+md(buf);}}
  else if(j.t==='error'){{d.innerHTML='<small>Denzyx</small><br><i>Error:</i> '+esc2(j.d);}}
  else if(j.t==='done'){{if(!buf)d.innerHTML='<small>Denzyx</small><br>'+esc2(j.d||'');}}
 }}
 msgs.scrollTop=msgs.scrollHeight;
}}
if(!buf){{d.innerHTML='<small>Denzyx</small><br><i>(tidak ada jawaban)</i>';}}
}};
</script>""")


def _status_page(m):
    st = member_status(m)
    badge = f'<span class="badge {st}">{st}</span>'
    pay = ""
    if st == "pending":
        cfg = load_config()
        pay = f"""<div class="paycard">{_qr_html(cfg)}<b>Akun belum aktif</b><br>
1. Scan QR di atas lalu transfer <b>Rp {cfg.get('price_idr'):,}</b><br>
2. Setelah transfer, klik tombol di bawah untuk konfirmasi ke Telegram<br>
3. Setelah dikonfirmasi, akun otomatis aktif
{_pay_tg_link(cfg, m)}
<p><small>Rp {cfg.get('price_idr'):,} / {cfg.get('sub_days')} hari — transfer & kirim bukti, akun otomatis aktif setelah dikonfirmasi</small></p></div>"""
    return page("Status", f"""<div class="card"><h3>Status Langganan</h3>
{pay}
<p>Username: <b>{html_esc(m['username'])}</b> {badge}</p>
<p>Nama: {html_esc(m.get('display_name', '-'))}</p>
<p>Aktif s/d: <b>{html_esc(m.get('expires_at') or '-')}</b></p>
<p>Terdaftar: {html_esc(m.get('created_at'))}</p>
<p><small><a href="/chat">← ke Chat</a> · <a href="/password">Ganti Password</a></small></p></div>""")


def _owner_page(cfg, msg="", err="", q="", pg=1):
    q = (q or "").strip().lower()
    per = int(cfg.get("owner_per_page") or 50)
    rows = []
    admins = 0
    all_m = list_members()
    if q:
        all_m = [m for m in all_m
                 if q in m.get("username", "").lower()
                 or q in (m.get("display_name") or "").lower()]
    total = len(all_m)
    pages = max(1, -(-total // per))
    pg = max(1, min(int(pg or 1), pages))
    start = (pg - 1) * per
    for m in all_m[start:start + per]:
        st = member_status(m)
        badge = f'<span class="badge {st}">{st}</span>'
        role_tag = (' <span class="badge admin">ADMIN</span>'
                    if is_admin(m) else '')
        if is_admin(m):
            admins += 1
        rows.append(f"<tr><td>{html_esc(m['username'])}{role_tag}</td>"
                    f"<td>{html_esc(m.get('display_name', '-'))}</td>"
                    f"<td>{badge}</td><td>{html_esc(m.get('expires_at') or '-')}</td>"
                    f"<td><a href='/owner/member/{html_esc(m['username'])}'>detail</a></td></tr>")
    table = ("<table><tr><th>username</th><th>nama</th><th>status</th>"
             "<th>aktif s/d</th><th></th></tr>" + "".join(rows) + "</table>")
    qsafe = html_esc(q)
    search = (f'<form method="get" action="/owner" class="toolbar-search">'
              f'<input name="q" value="{qsafe}" placeholder="cari username/nama..." '
              f'style="width:260px;display:inline-block;margin:0">'
              f'<button type="submit" style="width:auto;display:inline-block;padding:10px 16px;margin:0">Cari</button></form>')
    nav = ""
    if pages > 1:
        prev = max(1, pg - 1)
        nxt = min(pages, pg + 1)
        psep = f"&amp;q={urllib.parse.quote(q)}" if q else ""
        nav = (f'<p><small>Hal {pg}/{pages} ({total} member) — '
               f'<a href="/owner?pg={prev}{psep}">← Prev</a> · '
               f'<a href="/owner?pg={nxt}{psep}">Next →</a></small></p>')
    return page("Owner Panel", f"""<div class="toolbar">
 <a href="/owner">Dashboard</a><a href="/owner/logs">Log</a>
 <a href="/owner/security">Keamanan</a>
 <a href="/owner/visitors">Pengunjung</a>
 <a href="/owner/register">+ Daftarkan Member</a></div>
{_flash(msg, err)}
<div class="card"><h3>Owner Panel</h3>
<p>Member: {total} · Admin: {admins} · Server: {html_esc(cfg.get('host'))}:{cfg.get('port')}
 · Harga: Rp {cfg.get('price_idr'):,} / {cfg.get('sub_days')} hari</p>
{search}</div>
<div class="card">{table}{nav}</div>""", subtitle="owner")


def _admin_add_page(cfg, msg="", err=""):
    body = f"""<div class="card"><h3>Tambah Member Baru (reseller)</h3>
<p><small>Member langsung AKTIF, durasi <b>Rp {cfg.get('price_idr'):,} / {cfg.get('sub_days')} hari</b>.</small></p>
{_flash(msg, err)}
<form method="post" action="/admin/add">
<input type="hidden" name="_csrf" value="__CSRF__">
<input name="username" placeholder="username (login)" required>
<input name="display_name" placeholder="nama panggilan">
<input name="password" type="password" placeholder="password" required>
<input name="days" value="{cfg.get('sub_days')}" style="width:70px">
<button type="submit">Buat Member</button></form></div>"""
    return page("Tambah Member", f"""<div class="toolbar">
<a href="/chat">Chat</a><a href="/status">Status Langganan</a>
<a href="/admin/add">+ Tambah Member</a></div>
{body}""", subtitle="admin")


def _owner_security_page(msg="", err=""):
    """Halaman WAF: daftar IP yang diblokir + tombol unban."""
    bans = waf.list_bans()
    rows = []
    for ip, e in sorted(bans.items(),
                        key=lambda kv: kv[1].get("first_seen_ts") or 0,
                        reverse=True):
        csrf = '<input type="hidden" name="_csrf" value="__CSRF__">'
        rows.append(
            f"<tr><td><code>{html_esc(ip)}</code></td>"
            f"<td>{html_esc(e.get('reason') or '-')}</td>"
            f"<td>{int(e.get('count') or 1)}x</td>"
            f"<td>{html_esc(e.get('first_seen') or '-')}</td>"
            f"<td>{html_esc(e.get('last_seen') or '-')}</td>"
            f"<td>{html_esc(e.get('geo') or '-')}</td>"
            f"<td><form method='post' style='display:inline'>{csrf}"
            f"<input type='hidden' name='ip' value='{html_esc(ip)}'>"
            f"<button name='action' value='unban' "
            f"style='width:auto;display:inline-block;padding:8px 14px'>"
            f"Unban</button></form></td></tr>")
    table = ("<table><tr><th>IP</th><th>alasan</th><th>jumlah</th>"
             "<th>pertama kali</th><th>terakhir</th><th>lokasi</th><th></th></tr>"
             + "".join(rows) + "</table>") if rows else \
        "<p>✅ Tidak ada IP yang diblokir.</p>"
    return page("Keamanan Web", f"""<div class="toolbar">
<a href="/owner">Dashboard</a><a href="/owner/logs">Log</a>
<a href="/owner/security">Keamanan</a>
<a href="/owner/visitors">Pengunjung</a>
<a href="/owner/register">+ Daftarkan Member</a></div>
{_flash(msg, err)}
<div class="card"><h3>🛡️ IP yang Diblokir WAF</h3>
<p><small>IP di-ban otomatis saat ada serangan (scanner, brute-force, dll) —
di-banner permanen. Unban dari sini atau via bot: /unbanip &lt;ip&gt;.</small></p>
{table}</div>""", subtitle="owner")


def _visitor_badges(v, bans):
    """Badge IP class / bot / banned untuk baris pengunjung."""
    cls = v.get("ip_class") or "invalid"
    colors = {"public": "#22c55e", "private": "#f59e0b",
              "loopback": "#8b5cf6", "invalid": "#ef4444"}
    lbl = {"public": "PUBLIC", "private": "PRIVATE",
           "loopback": "LOOPBACK", "invalid": "?"}[cls]
    out = [f'<span class="badge" style="background:{colors[cls]}">{lbl}</span>']
    if v.get("is_bot"):
        out.append('<span class="badge" style="background:#7c3aed">BOT</span>')
    if v.get("ip") in bans:
        out.append('<span class="badge" style="background:#ef4444">BANNED</span>')
    return " ".join(out)


def _owner_visitors_page(msg="", err="", q="", pg=1, sort="last"):
    """Halaman owner: semua pengunjung (IP, lokasi, software, dll)."""
    q = (q or "").strip().lower()
    track.flush()
    bans = waf.list_bans()
    data = track.load()
    items = list(data.values())
    if q:
        items = [v for v in items
                 if q in v.get("ip", "").lower()
                 or q in (v.get("browser") or "").lower()
                 or q in (v.get("os") or "").lower()
                 or q in (v.get("geo") or "").lower()
                 or q in (v.get("device") or "").lower()]
    if sort == "visits":
        items.sort(key=lambda v: int(v.get("visits") or 0), reverse=True)
    elif sort == "new":
        items.sort(key=lambda v: v.get("first_seen") or "", reverse=True)
    else:
        items.sort(key=lambda v: v.get("last_seen") or "", reverse=True)
    s = track.summary()
    total = len(items)
    per = int(load_config().get("owner_per_page") or 50)
    pages = max(1, -(-total // per))
    pg = max(1, min(int(pg or 1), pages))
    rows = []
    start = (pg - 1) * per
    for v in items[start:start + per]:
        ip = html_esc(v.get("ip", ""))
        sw = f"{html_esc(v.get('browser') or '-')} · {html_esc(v.get('os') or '-')}"
        loc = html_esc(v.get("geo") or "-")
        isp = html_esc(v.get("isp") or "")
        if isp and v.get("geo"):
            loc += f" <small>({isp})</small>"
        paths = " ".join(f"<code>{html_esc(p)}</code>" for p in (v.get("paths") or [])[:4])
        csrf = '<input type="hidden" name="_csrf" value="__CSRF__">'
        rows.append(
            f"<tr><td>{_visitor_badges(v, bans)} <code>{ip}</code><br>"
            f"<small>{html_esc(v.get('cf_ip') or '-')} · "
            f"{html_esc(v.get('peer') or '-')}</small></td>"
            f"<td>{loc}</td><td>{sw}<br><small>{html_esc(v.get('device') or '-')}"
            f"{' · ' + html_esc(v.get('engine') or '-') if v.get('engine') and v.get('engine') != '-' else ''}</small></td>"
            f"<td>{int(v.get('visits') or 0)}x</td>"
            f"<td><small>{html_esc(v.get('first_seen') or '-')}<br>"
            f"{html_esc(v.get('last_seen') or '-')}</small></td>"
            f"<td><small>{paths}</small></td>"
            f"<td><a href='/owner/visitor/{urllib.parse.quote(v.get('ip',''))}'>detail</a>"
            f" <form method='post' style='display:inline'>{csrf}"
            f"<input type='hidden' name='ip' value='{html_esc(v.get('ip',''))}'>"
            f"<button name='action' value='ban' style='width:auto;display:inline-block;padding:6px 10px;margin:0'>⛔ Ban</button>"
            f"</form></td></tr>")
    table = ("<table><tr><th>IP / peer</th><th>lokasi & ISP</th>"
             "<th>software</th><th>kunjungan</th><th>pertama / terakhir</th>"
             "<th>path</th><th></th></tr>" + "".join(rows) + "</table>") if rows \
        else "<p>Belum ada pengunjung tercatat.</p>"
    qsafe = html_esc(q)
    search = (f'<form method="get" action="/owner/visitors" class="toolbar-search">'
              f'<input name="q" value="{qsafe}" placeholder="cari IP/browser/OS/lokasi..." '
              f'style="width:280px;display:inline-block;margin:0">'
              f'<button type="submit" style="width:auto;display:inline-block;padding:10px 16px;margin:0">Cari</button></form>')
    psep = f"&amp;q={urllib.parse.quote(q)}" if q else ""
    nav = ""
    if pages > 1:
        prev, nxt = max(1, pg - 1), min(pages, pg + 1)
        nav = (f'<p><small>Hal {pg}/{pages} — '
               f'<a href="/owner/visitors?pg={prev}{psep}">← Prev</a> · '
               f'<a href="/owner/visitors?pg={nxt}{psep}">Next →</a></small></p>')
    cards = (f'<div class="toolbar" style="gap:8px;flex-wrap:wrap">'
             f'<span class="badge" style="background:#22c55e">Total {s["total"]}</span>'
             f'<span class="badge" style="background:#4f7cff">Hari ini {s["today"]}</span>'
             f'<span class="badge" style="background:#8b5cf6">Aktif 24 jam {s["active_24h"]}</span>'
             f'<span class="badge" style="background:#64748b">Kunjungan {s["visits"]}</span>'
             f'<span class="badge" style="background:#ef4444">Bot {s["bots"]}</span>'
             f'<span class="badge" style="background:#f59e0b">Mobile {s["mobile"]}</span>'
             f'</div>')
    csrf = '<input type="hidden" name="_csrf" value="__CSRF__">'
    return page("Pengunjung", f"""<div class="toolbar">
<a href="/owner">Dashboard</a><a href="/owner/logs">Log</a>
<a href="/owner/security">Keamanan</a>
<a href="/owner/visitors">Pengunjung</a>
<a href="/owner/register">+ Daftarkan Member</a></div>
{_flash(msg, err)}
<div class="card"><h3>👁️ Pengunjung Web</h3>
<p><small>Semua yang masuk ke web: IP public/private, lokasi & ISP,
software (browser/OS/device), bahasa, screen, timezone, CPU/memori,
riwayat login. Data terenkripsi (Fernet) di
<code>webdata/visitors.json</code> — tidak terbaca mentah.</small></p>
{cards}
{search}</div>
<div class="card">{table}{nav}
<form method="post" style="margin-top:12px">{csrf}
<button name="action" value="clear" class="err"
style="width:auto;display:inline-block;padding:8px 14px">🗑️ Hapus semua data pengunjung</button>
</form></div>""", subtitle="owner")


def _owner_visitor_page(ip, msg="", err=""):
    track.flush()
    v = track.get(ip)
    if not v:
        return page("Visitor", f"""<div class="toolbar">
<a href="/owner">Dashboard</a><a href="/owner/visitors">← Pengunjung</a></div>
{_flash(msg, err)}
<div class="card"><h3>👁️ Visitor {html_esc(ip)}</h3>
<p>Data tidak ditemukan.</p></div>""", subtitle="owner")
    bans = waf.list_bans()
    rows = [
        ("IP", v.get("ip", "")),
        ("Klasifikasi", v.get("ip_class", "")),
        ("Peer (socket)", v.get("peer", "")),
        ("CF-Connecting-IP", v.get("cf_ip", "")),
        ("X-Forwarded-For", v.get("xff", "")),
        ("Lokasi", v.get("geo", "-")),
        ("ISP", v.get("isp", "-")),
        ("Organisasi", v.get("org", "-")),
        ("Tipe koneksi", v.get("conn_type", "-")),
        ("User-Agent", v.get("ua", "-")),
        ("Browser", v.get("browser", "-")),
        ("OS", v.get("os", "-")),
        ("Device", v.get("device", "-")),
        ("Engine", v.get("engine", "-")),
        ("Bot?", "Ya" if v.get("is_bot") else "Tidak"),
        ("Bahasa (Accept-Language)", v.get("lang", "-") or "-"),
        ("Do-Not-Track", "Ya" if v.get("dnt") else "Tidak"),
        ("Screen", v.get("screen", "-") or "-"),
        ("Timezone", v.get("tz", "-") or "-"),
        ("Memory", (v.get("mem_gb", "-") or "-") + " GB"),
        ("CPU cores", v.get("cpu_cores", "-") or "-"),
        ("Baterai", (v.get("battery", "-") or "-") + "%"),
        ("Pertama kali", v.get("first_seen", "-")),
        ("Terakhir", v.get("last_seen", "-")),
        ("Total kunjungan", str(v.get("visits", 0))),
    ]
    if v.get("last_login"):
        rows.append(("Login terakhir", f"{v['last_login']} — {v.get('last_login_user') or '-'}"))
    if v.get("last_fail"):
        rows.append(("Login gagal terakhir", f"{v['last_fail']} — {v.get('last_fail_user') or '-'}"))
    if v.get("ip") in bans:
        rows.append(("Status WAF", "⛔ BANNED — " + (bans[v["ip"]].get("reason") or "")))
    tbl = "<table>" + "".join(
        f"<tr><td style='width:200px'><small>{html_esc(k)}</small></td>"
        f"<td>{html_esc(str(val))}</td></tr>" for k, val in rows) + "</table>"
    methods = " · ".join(f"<code>{html_esc(m)}</code> {c}x"
                         for m, c in (v.get("methods") or {}).items()) or "-"
    statuses = " · ".join(f"<code>{html_esc(s)}</code> {c}x"
                          for s, c in (v.get("statuses") or {}).items()) or "-"
    hints = " · ".join(f"<code>{html_esc(k)}</code> {html_esc(val)}"
                       for k, val in (v.get("client_hints") or {}).items()) or "-"
    logins = "".join(
        f"<li><small>{html_esc(e.get('ts') or '')}</small> "
        f"{'✅' if e.get('ok') else '❌'} <code>{html_esc(e.get('user') or '-')}</code></li>"
        for e in (v.get("login_events") or [])) or "<li>-</li>"
    paths = "".join(f"<li><code>{html_esc(p)}</code></li>"
                    for p in (v.get("paths") or [])) or "<li>-</li>"
    refs = "".join(f"<li><small>{html_esc(r)}</small></li>"
                   for r in (v.get("referers") or [])) or "<li>-</li>"
    log_rows = ""
    for j in track.recent(ip, 20):
        log_rows += (f"<tr><td><small>{html_esc(j.get('ts') or '')}</small></td>"
                     f"<td><code>{html_esc(j.get('method') or '')}</code> "
                     f"{html_esc(j.get('path') or '')}</td>"
                     f"<td><small>{html_esc(j.get('ref') or '-')}</small></td></tr>")
    log_tbl = ("<table><tr><th>waktu</th><th>request</th><th>referer</th></tr>"
               + log_rows + "</table>") if log_rows else "<p>-</p>"
    csrf = '<input type="hidden" name="_csrf" value="__CSRF__">'
    ban_form = ""
    if v.get("ip") in bans:
        ban_form = f"""<form method="post" style="display:inline">{csrf}
<input type="hidden" name="ip" value="{html_esc(v.get('ip',''))}">
<button class="ok" name="action" value="unban"
style="width:auto;display:inline-block;padding:8px 14px">Unban</button></form>"""
    else:
        ban_form = f"""<form method="post" style="display:inline">{csrf}
<input type="hidden" name="ip" value="{html_esc(v.get('ip',''))}">
<button class="err" name="action" value="ban"
style="width:auto;display:inline-block;padding:8px 14px">⛔ Ban IP ini</button></form>"""
    return page("Visitor", f"""<div class="toolbar">
<a href="/owner">Dashboard</a><a href="/owner/logs">Log</a>
<a href="/owner/security">Keamanan</a>
<a href="/owner/visitors">← Pengunjung</a></div>
{_flash(msg, err)}
<div class="card"><h3>👁️ Detail Visitor</h3>{tbl}</div>
<div class="card"><h3>🛡️ Aksi</h3>{ban_form}</div>
<div class="card"><h3>Metode & Status</h3>
<p><small>Metode:</small> {methods}</p>
<p><small>Status:</small> {statuses}</p></div>
<div class="card"><h3>Client Hints</h3><p><small>{hints}</small></p></div>
<div class="card"><h3>Riwayat Login</h3><ul>{logins}</ul></div>
<div class="card"><h3>Path yang dikunjungi</h3><ul>{paths}</ul></div>
<div class="card"><h3>Referer</h3><ul>{refs}</ul></div>
<div class="card"><h3>Riwayat terakhir</h3>{log_tbl}</div>""", subtitle="owner")


def _owner_member_page(m):
    st = member_status(m)
    pw = "-"
    try:
        pw = dec_secret(m["password"])
    except Exception:  # noqa: BLE001
        pw = "(tidak bisa didecrypt)"
    csrf = '<input type="hidden" name="_csrf" value="__CSRF__">'
    act = f"""<form method="post" style="display:inline">{csrf}
<button class="ok" name="action" value="activate">Aktivasi 30 hari</button></form>
<form method="post" style="display:inline">{csrf}
<button name="action" value="ban">Ban</button></form>
<form method="post" style="display:inline">{csrf}
<button class="ok" name="action" value="unban">Unban</button></form>
<form method="post" style="display:inline">{csrf}
<input name="days" value="30" style="width:70px;display:inline">
<button name="action" value="extend">Perpanjang (hari)</button></form>
"""
    if m.get("role") == "admin":
        act += (f'<form method="post" style="display:inline">{csrf}'
                '<button name="action" value="demote">Turunkan jadi member</button></form>')
    else:
        act += (f'<form method="post" style="display:inline">{csrf}'
                '<button class="ok" name="action" value="makeadmin">Jadikan Admin</button></form>')
    uname = html_esc(m["username"])
    del_form = (f'<form method="post" onsubmit="return confirm('
                f'\'Hapus {uname} PERMANEN? Data chat &amp; password ikut terhapus.\');">{csrf}'
                '<button class="danger" name="action" value="delete">🗑️ Hapus Pengguna (permanen)</button></form>')
    reset_pw = f"""<form method="post">{csrf}
<input name="new_password" type="password" placeholder="password baru (min. 4)" autocomplete="off">
<button name="action" value="resetpass">Reset Password</button></form>"""
    return page(f"Member {m['username']}", f"""<div class="card">
<a href="/owner">← Owner Panel</a>
<h3>Member: {html_esc(m['username'])} <span class="badge {st}">{st}</span></h3>
<p>Role: <b>{html_esc(m.get('role') or 'member')}</b></p>
<p>Nama: {html_esc(m.get('display_name', '-'))}</p>
<p>Password (encrypt di simpan, ini buat owner): <code>{html_esc(pw)}</code></p>
<p>Aktif s/d: <b>{html_esc(m.get('expires_at') or '-')}</b></p>
<p>Terdaftar: {html_esc(m.get('created_at'))} · IP: {html_esc(m.get('ip') or '-')}</p>
<p>Login: {m.get('login_count', 0)}x · terakhir {html_esc(m.get('last_login') or '-')}</p>
<p>Catatan: {html_esc(m.get('note') or '-')}</p>
{act}</div>
<div class="card"><h4>Zona berbahaya</h4>{del_form}
<p><small>Menghapus pengguna akan menghilangkan akun, chat history, dan sesi login-nya secara permanen. Tidak bisa dibatalkan.</small></p></div>
<div class="card"><h4>Reset password</h4>{reset_pw}</div>
<div class="card"><h4>Sesi aktif</h4><pre>{html_esc(json.dumps(m.get('sessions') or [], indent=2, ensure_ascii=False))}</pre></div>
<div class="card"><h4>Riwayat chat</h4>
<a href="/owner/member/{html_esc(m['username'])}/md">Lihat file md sesi</a></div>""",
                 subtitle="owner")


def _owner_logs_page(q="", pg=1):
    q = (q or "").strip().lower()
    per = 100
    blocks = {}
    for name, label in (("register", "Registrasi (username + password)"),
                        ("login", "Login"),
                        ("admin", "Aktivitas admin")):
        rows = read_log(name, 500)
        if q:
            rows = [r for r in rows if q in r.lower()]
        total = len(rows)
        pages = max(1, -(-total // per))
        pg = max(1, min(int(pg or 1), pages))
        start = (pg - 1) * per
        body = "<br>".join(html_esc(x) for x in rows[start:start + per]) or "-"
        nav = ""
        if pages > 1:
            psep = f"&amp;q={urllib.parse.quote(q)}" if q else ""
            nav = (f'<p><small>Hal {pg}/{pages} — '
                   f'<a href="/owner/logs?pg={max(1, pg - 1)}{psep}">← Prev</a> · '
                   f'<a href="/owner/logs?pg={min(pages, pg + 1)}{psep}">Next →</a></small></p>')
        blocks[name] = f'<div class="card"><h3>{label}</h3><pre>{body}</pre>{nav}</div>'
    qsafe = html_esc(q)
    search = (f'<form method="get" action="/owner/logs" class="toolbar-search">'
              f'<input name="q" value="{qsafe}" placeholder="cari di log..." '
              f'style="width:260px;display:inline-block;margin:0">'
              f'<button type="submit" style="width:auto;display:inline-block;padding:10px 16px;margin:0">Cari</button></form>')
    return page("Log Owner", f"""<a href="/owner">← Owner Panel</a>
{search}
{blocks['register']}
{blocks['login']}
{blocks['admin']}""",
                 subtitle="owner")


def _password_page(msg="", err=""):
    body = f"""<div class="auth"><div class="card authcard">
<div class="auth-hero"><div class="logo">🔏</div>
<h3>Ganti Password</h3>
<small>Ganti password login member kamu</small></div>
{_flash(msg, err)}
<form method="post" action="/password">
<input type="hidden" name="_csrf" value="__CSRF__">
<label>Password lama</label>
<input name="old_password" type="password" placeholder="password saat ini" required autocomplete="current-password">
<label>Password baru</label>
<input name="new_password" type="password" placeholder="min. 4 karakter" required autocomplete="new-password">
<label>Ulangi password baru</label>
<input name="confirm_password" type="password" placeholder="ketik ulang" required autocomplete="new-password">
<button type="submit">Simpan Password Baru →</button></form>
<div class="auth-switch"><a href="/status">← ke Status</a></div>
</div></div>"""
    return page("Ganti Password", body)


def html_esc(s):
    import html
    return html.escape(str(s))


def _md_inline(s):
    import html as _html
    import re
    s = _html.escape(s)
    s = re.sub(r"`([^`]+)`", r"<code>\1</code>", s)
    s = re.sub(r"\*\*([^*]+)\*\*", r"<b>\1</b>", s)
    s = re.sub(r"\*([^*]+)\*", r"<i>\1</i>", s)
    s = re.sub(r"\[([^\]]+)\]\((https?://[^)\s]+)\)",
               r'<a href="\2" target="_blank" rel="noopener">\1</a>', s)
    s = re.sub(r"(^|\s)(https?://[^\s<]+)",
               r'\1<a href="\2" target="_blank" rel="noopener">\2</a>', s)
    return s


def _md_html(text):
    """Escape + render markdown dasar ke HTML aman (tanpa XSS)."""
    lines = str(text).split("\n")
    out, i = [], 0
    while i < len(lines):
        ln = lines[i]
        stripped = ln.lstrip()
        if stripped.startswith("```"):
            buf, i = [], i + 1
            while i < len(lines) and not lines[i].lstrip().startswith("```"):
                buf.append(lines[i])
                i += 1
            i += 1
            out.append("<pre><code>" + html_esc("\n".join(buf)) + "</code></pre>")
            continue
        if not stripped:
            out.append("")
            i += 1
            continue
        if ln.startswith("### "):
            out.append("<h4>" + _md_inline(ln[4:]) + "</h4>")
        elif ln.startswith("## "):
            out.append("<h4>" + _md_inline(ln[3:]) + "</h4>")
        elif ln.startswith("# "):
            out.append("<h3>" + _md_inline(ln[2:]) + "</h3>")
        elif stripped[:2] in ("- ", "* ", "+ "):
            items, i = [], i + 1
            while True:
                items.append(_md_inline(lines[i - 1].lstrip()[2:]))
                if i >= len(lines) or lines[i].lstrip()[:2] not in ("- ", "* ", "+ "):
                    break
                i += 1
            out.append("<ul>" + "".join(f"<li>{it}</li>" for it in items) + "</ul>")
        else:
            out.append(_md_inline(ln))
            i += 1
    return "\n".join(out)


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    server_version = "denzyx"      # jangan bocorkan BaseHTTP/Python version
    sys_version = ""

    def setup(self):
        super().setup()
        # matikan slowloris / koneksi menggantung (read/write timeout)
        try:
            self.connection.settimeout(20)
        except OSError:
            pass

    def handle_one_request(self):
        """Jalankan SATU request lalu lepas slot koneksi.

        Dengan HTTP/1.1 keep-alive, satu koneksi menangani banyak request.
        _prelude() menambah slot di enter() per request — tanpa release
        per-request, counter naik terus dan semua request berikutnya kena 429
        "terlalu banyak koneksi". Release di sini, bukan di finish().
        """
        try:
            super().handle_one_request()
        finally:
            ip = getattr(self, "_real_ip", None)
            if ip and getattr(self, "_conn_in", False):
                self._conn_in = False
                _CONN.exit(ip)

    def finish(self):
        try:
            super().finish()
        finally:
            ip = getattr(self, "_real_ip", None)
            if ip and getattr(self, "_conn_in", False):
                self._conn_in = False
                _CONN.exit(ip)

    # --- helpers ---
    def _cors(self, headers):
        out = dict(headers or {})
        cfg = load_config()
        origins = [str(x).strip() for x in (cfg.get("cors_origins") or [])
                   if str(x).strip()]
        origin = self.headers.get("Origin") or ""
        if origins:
            # allowlist ketat: hanya origin terdaftar yang boleh akses lintas-origin
            if origin in origins:
                out["Access-Control-Allow-Origin"] = origin
            else:
                out["Access-Control-Allow-Origin"] = ""
        else:
            # tanpa allowlist, tetap layani CORS (frontend vercel default)
            out.setdefault("Access-Control-Allow-Origin", "*")
        out.setdefault("Access-Control-Allow-Methods",
                       "GET, POST, OPTIONS")
        out.setdefault("Access-Control-Allow-Headers", "Content-Type")
        out.setdefault("Access-Control-Max-Age", "600")
        return out

    def _send(self, code, body=b"", ctype="text/html; charset=utf-8",
              headers=None):
        ip = getattr(self, "_real_ip", None)
        if ip:
            track.status(ip, code)
        headers = self._cors(headers)
        headers.setdefault("X-Content-Type-Options", "nosniff")
        headers.setdefault("X-Frame-Options", "DENY")
        headers.setdefault("Referrer-Policy", "no-referrer")
        headers.setdefault("Cross-Origin-Opener-Policy", "same-origin")
        if ctype.startswith("text/html"):
            headers.setdefault("Cache-Control", "no-store")
            headers.setdefault(
                "Content-Security-Policy",
                "default-src 'self'; img-src 'self' data: https:; "
                "style-src 'self' 'unsafe-inline'; script-src 'self' "
                "'unsafe-inline'; connect-src 'self' https:; "
                "frame-ancestors 'none'")
            headers.setdefault("Permissions-Policy",
                               "camera=(), microphone=(), geolocation=(), "
                               "payment=()")
        if getattr(self, "_secure", False):
            headers.setdefault("Strict-Transport-Security",
                               "max-age=31536000; includeSubDomains")
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        for k, v in headers.items():
            self.send_header(k, v)
        self.end_headers()
        if body:
            self.wfile.write(body)

    def _json(self, code, obj, headers=None):
        self._send(code, json.dumps(obj, ensure_ascii=False).encode(),
                   "application/json; charset=utf-8", headers)

    def _wants_json(self):
        return "json" in self.headers.get("Accept", "")

    def _cookies(self):
        c = SimpleCookie()
        raw = self.headers.get("Cookie")
        if raw:
            c.load(raw)
        return {k: m.value for k, m in c.items()}

    def _form(self):
        raw = self.headers.get("Content-Length") or "0"
        try:
            ln = int(raw)
        except ValueError:
            raise _ReqError(400, b"Content-Length tidak valid")
        if ln < 0:
            raise _ReqError(400, b"Content-Length tidak valid")
        max_body = int(load_config().get("max_body") or 262144)
        if ln > max_body:
            raise _ReqError(413, b"body terlalu besar")
        body = self.rfile.read(ln) if ln else b""
        ctype = self.headers.get("Content-Type", "")
        if "json" in ctype:
            try:
                return json.loads(body.decode("utf-8") or "{}")
            except json.JSONDecodeError:
                return {}
        return {k: v[0] for k, v in
                urllib.parse.parse_qs(body.decode("utf-8")).items()}

    def _query(self):
        q = urllib.parse.parse_qs(urllib.parse.urlparse(self.path).query)
        return {k: v[0] for k, v in q.items()}

    def _ip(self):
        return getattr(self, "_real_ip", str(self.client_address[0]))

    def _event_notify(self, title, username, subtitle="", footer=""):
        """Notifikasi event TG detail (registrasi/login) — thread background.

        Merangkum IP publik/private + peer, geolokasi & ISP, perangkat/
        software (dari User-Agent), dan waktu lengkap (tanggal-jam-tahun).
        Non-blocking: geolokasi & kirim pesan jalan di thread terpisah.
        """
        import track as _track

        def _build():
            ip = self._ip()
            peer = str(self.client_address[0])
            ua = self.headers.get("User-Agent", "")
            p = _track.parse_ua(ua)
            cls = _track.ip_class(ip)
            cls_label = {"public": "Publik", "private": "Private",
                         "loopback": "Loopback", "invalid": "?"}.get(
                             cls, cls)
            now = time.strftime("%a, %d %b %Y %H:%M:%S", time.localtime())
            tz = time.strftime("%Z") or "WIB"
            bits = [b for b in (p["browser"], p["os"], p["device"]) if b != "-"]
            dev = " · ".join(bits) or "-"
            bot = "YA 🤖" if p["is_bot"] else "Tidak"
            ref = str(self.headers.get("Referer") or "-")[:200]
            lines = [
                title,
                f"👤 Username : {username}",
            ]
            if subtitle:
                lines.append(subtitle)
            lines += [
                f"🕐 Waktu    : {now} ({tz})",
                "",
                f"🌐 IP Publik: {ip}  [ {cls_label} ]",
                f"🖧 Peer     : {peer}",
                f"🛡 CF-IP    : {self.headers.get('CF-Connecting-IP') or '-'}",
                f"➡ XFF      : {self.headers.get('X-Forwarded-For') or '-'}",
                "",
                f"💻 Software : {dev}",
                f"🔍 Engine   : {p['engine'] or '-'}",
                f"🤖 Bot      : {bot}",
                f"🔗 Referer  : {ref}",
            ]
            # lokasi & ISP (geolokasi IP publik, non-blocking)
            if _track._geo_ok():
                geo = waf.geo_info(ip) or {}
                loc = geo.get("loc") or "-"
                isp = geo.get("isp") or "-"
                org = geo.get("org") or "-"
                conn = geo.get("type") or "-"
                if loc != "-":
                    lines.insert(7, f"📍 Lokasi   : {loc}")
                lines.insert(8, f"🏢 ISP      : {isp}")
                if org and org != isp:
                    lines.insert(9, f"🗄 Org      : {org}")
                lines.insert(10, f"🔌 Tipe     : {conn}")
            if footer:
                lines.append("")
                lines.append(footer)
            return "\n".join(lines)

        def _send():
            try:
                from denzbot import tg_notify
                tg_notify(_build())
            except Exception:  # noqa: BLE001
                pass

        threading.Thread(target=_send, daemon=True).start()

    def _reg_notify(self, username, display):
        self._event_notify(
            "📝 REGISTRASI BARU", username,
            subtitle=f"🪪 Nama     : {display or '-'}",
            footer="⚠️ Cek owner panel untuk aktivasi.")

    def _login_notify(self, username):
        self._event_notify("🔓 LOGIN MEMBER", username,
                           footer="⚠️ Bukan kamu? Hubungi owner.")

    # --- waf / keamanan ---
    def _prelude(self):
        """Set state per-request: IP asli, path, guard WAF dasar.

        Return True bila request sudah ditangani (jangan lanjut routing).
        """
        global _HTTPS
        self._path = urllib.parse.urlparse(self.path).path
        self._real_ip = waf.get_real_ip(self.client_address, self.headers)
        cfg = load_config()
        # perekam pengunjung: IP (public/private), lokasi, software, dll.
        if cfg.get("track_visitors", True) and self._path != "/api/ping":
            track.set_geo(cfg.get("track_geo", True))
            track.visit(self._real_ip, self.headers, self._path,
                        method=self.command, peer=self.client_address)
        # edge TLS (SSL langsung / lewat cloudflared) → Secure cookie + HSTS
        import ssl
        self._secure = bool(
            isinstance(getattr(self, "connection", None), ssl.SSLSocket)
            or self.headers.get("CF-Connecting-IP")
            or (self.headers.get("X-Forwarded-Proto") or "").lower() == "https")
        if self._secure:
            _HTTPS = True
        if not _CONN.enter(self._real_ip):
            self._json(429, {"error": "terlalu banyak koneksi, coba lagi"})
            return True
        self._conn_in = True
        # host header smuggling / Host asing (allowlist opsional)
        host = self.headers.get("Host") or ""
        if "\r" in host or "\n" in host:
            self._waf_deny("Host header tidak valid")
            return True
        allowed = [str(x).strip().lower() for x in (cfg.get("allowed_hosts") or [])
                   if str(x).strip()]
        if allowed:
            hostname = host.split(":", 1)[0].strip().lower()
            if hostname not in allowed and hostname not in (
                    "localhost", "127.0.0.1", "::1"):
                self._waf_deny("Host tidak diizinkan")
                return True
        # rate limit request umum per IP (bot / crawl)
        if not self._rate_limit(f"req:{self._real_ip}",
                                cfg.get("req_rate_max", 600),
                                cfg.get("req_rate_window", 60)):
            self._json(429, {"error": "terlalu banyak request, coba lagi"})
            return True
        return False

    def _waf_guard(self):
        """Cek IP banned + sinyal serangan. Return True bila sudah direspon."""
        ip = self._ip()
        if waf.is_banned(ip):
            self._waf_deny("IP ini diblokir WAF")
            return True
        if not load_config().get("waf", True):
            return False
        sig = waf.scan_signal(ip, self.headers.get("User-Agent", ""),
                              self._path,
                              urllib.parse.urlparse(self.path).query)
        if sig:
            waf.ban(ip, sig, ua=self.headers.get("User-Agent", ""),
                    path=self._path)
            self._waf_deny(sig)
            return True
        return False

    def _waf_deny(self, reason):
        if self._wants_json():
            self._json(403, {"ok": False, "error": "akses diblokir"})
        else:
            self._send(403, _blocked_page(reason).encode())

    def _record_404(self):
        """Catat 404 path tidak dikenal; ban bila endpoint scan. Return handled."""
        if waf.record_404(self._ip(), self._path):
            self._waf_deny("endpoint scan")
            return True
        return False

    def _flag(self, kind, reason):
        """Pencatat mencurigakan; ban otomatis bila lewat threshold."""
        waf.flag(self._ip(), kind, reason)

    # --- csrf (per-sesi, HMAC di cookie HttpOnly) ---
    def _ensure_csrf(self):
        raw = getattr(self, "_csrf_raw", None)
        if not raw:
            raw = self._cookies().get("denz_csrf") or secrets.token_hex(16)
            self._csrf_raw = raw
        return raw

    def _csrf_tok(self):
        raw = self._ensure_csrf()
        secret = load_config().get("secret") or "x"
        return hmac.new(secret.encode(), raw.encode(), hashlib.sha256).hexdigest()

    def _csrf_ok(self, data):
        raw = self._cookies().get("denz_csrf")
        sub = str(data.get("_csrf") or "")
        if not raw or not sub:
            return False
        secret = load_config().get("secret") or "x"
        exp = hmac.new(secret.encode(), raw.encode(), hashlib.sha256).hexdigest()
        return secrets.compare_digest(exp, sub)

    def _html(self, body_bytes):
        """Kirim halaman HTML + pastikan cookie CSRF + injeksi token."""
        tok = self._csrf_tok()
        hdr = {"Set-Cookie": _cookie("denz_csrf", self._ensure_csrf(),
                                     secure=getattr(self, "_secure", False))}
        self._send(200, body_bytes.replace(b"__CSRF__", tok.encode()),
                   headers=hdr)

    def _rate_limit(self, key, max_hits, window_sec):
        cfg = load_config()
        return _RL.hit(key,
                       int(max_hits or cfg.get("rate_max_attempts", 8)),
                       int(window_sec or cfg.get("rate_window_sec", 600)))

    def _redirect(self, loc, headers=None):
        self._send(HTTPStatus.FOUND, b"", headers={"Location": loc,
                                                   **(headers or {})})

    def _auth_member(self):
        tok = self._cookies().get("denz_member")
        if not tok:
            auth = self.headers.get("Authorization", "")
            if auth.lower().startswith("bearer "):
                tok = auth[7:].strip()
        if not tok:
            return None, None
        return member_by_token(tok)

    def _auth_owner(self):
        return owner_token_valid(self._cookies().get("denz_owner"))

    def _auth_admin(self):
        """Member ber-role admin (reseller) yang sudah login."""
        m, _ = self._auth_member()
        if m and is_admin(m):
            return m
        return None

    # --- routes ---
    def do_OPTIONS(self):
        if self._prelude():
            return
        if self._waf_guard():
            return
        self._send(204, b"")

    def do_GET(self):
        try:
            if self._prelude():
                return
            if self._waf_guard():
                return
            self._do_get()
        except _ReqError as e:
            self._send(e.code, e.body, e.ctype, e.headers)
        except (BrokenPipeError, ConnectionResetError):
            pass

    def _do_get(self):
        cfg = load_config()
        path = self._path
        m, _ = self._auth_member()
        if path == "/":
            self._redirect("/chat" if m else "/login")
        elif path == "/api/status":
            self._api_status(m)
        elif path == "/api/me":
            self._api_me(m)
        elif path == "/login":
            self._html(_login_page(cfg).encode())
        elif path == "/register":
            self._html(_register_page(cfg).encode())
        elif path == "/qr":
            self._serve_qr(cfg)
        elif path == "/status":
            if not m:
                self._redirect("/login")
            else:
                self._html(_status_page(m).encode())
        elif path == "/chat":
            if not m:
                self._redirect("/login")
            else:
                self._html(_chat_page(m, cfg).encode())
        elif path == "/password":
            if not m:
                self._redirect("/login")
            else:
                self._html(_password_page().encode())
        elif path == "/admin/add":
            if not self._auth_admin():
                self._redirect("/chat")
            else:
                self._html(_admin_add_page(cfg).encode())
        elif path.startswith("/owner"):
            if not self._auth_owner():
                self._html(_owner_login_page(cfg).encode())
            else:
                self._owner_get(path, cfg)
        else:
            if self._record_404():
                return
            self._html(page("404", "<p>404</p>").encode())

    def do_POST(self):
        try:
            if self._prelude():
                return
            if self._waf_guard():
                return
            self._do_post()
        except _ReqError as e:
            if e.ctype.startswith("application/json"):
                self._json(e.code, {"ok": False, "error": "permintaan ditolak"})
            else:
                self._send(e.code, e.body, e.ctype, e.headers)
        except (BrokenPipeError, ConnectionResetError):
            pass

    def _do_post(self):
        cfg = load_config()
        path = self._path
        data = self._form()
        if path == "/api/register":
            self._api_register(data)
        elif path == "/api/login":
            self._api_login(data)
        elif path == "/api/ping":
            self._api_ping(data)
        elif path == "/api/chat":
            self._post_chat(data)
        elif path == "/api/chat/stream":
            self._post_chat_stream(data)
        elif path == "/logout":
            if self._csrf_ok(data):
                hdr = {"Set-Cookie": _cookie("denz_member", "", max_age=0,
                                             secure=getattr(self, "_secure", False))}
                self._redirect("/login", hdr)
            else:
                self._html(page("403", "<p>Permintaan tidak valid (CSRF).</p>").encode())
        elif path == "/login":
            if not self._csrf_ok(data):
                self._html(page("403", "<p>Permintaan tidak valid (CSRF).</p>").encode())
            else:
                self._post_login(cfg, data)
        elif path == "/register":
            if not self._csrf_ok(data):
                self._html(page("403", "<p>Permintaan tidak valid (CSRF).</p>").encode())
            else:
                self._post_register(cfg, data)
        elif path == "/password":
            if not self._csrf_ok(data):
                self._html(page("403", "<p>Permintaan tidak valid (CSRF).</p>").encode())
            else:
                self._post_password(data)
        elif path == "/admin/add":
            if not self._auth_admin():
                self._redirect("/chat")
            elif not self._csrf_ok(data):
                self._html(page("403", "<p>Permintaan tidak valid (CSRF).</p>").encode())
            else:
                self._post_admin_add(cfg, data)
        elif path == "/owner/login":
            if not self._csrf_ok(data):
                self._html(page("403", "<p>Permintaan tidak valid (CSRF).</p>").encode())
            else:
                self._post_owner_login(cfg, data)
        elif path.startswith("/owner"):
            if not self._auth_owner():
                self._redirect("/owner")
            elif not self._csrf_ok(data):
                self._html(page("403", "<p>Permintaan tidak valid (CSRF).</p>").encode())
            else:
                self._owner_post(path, data)
        else:
            if self._record_404():
                return
            self._html(page("404", "<p>404</p>").encode())

    # --- api json (untuk frontend vercel) ---
    def _api_register(self, data):
        cfg = load_config()
        if not self._rate_limit(f"reg:{self._ip()}", cfg.get("rate_max_attempts", 8),
                                cfg.get("rate_window_sec", 600)):
            self._flag("register", "spam registrasi (rate-limit)")
            self._json(429, {"ok": False, "error": "terlalu banyak percobaan, coba lagi nanti"})
            return
        username = str(data.get("username") or "").strip()
        display = str(data.get("display_name") or "").strip()
        password = str(data.get("password") or "")
        if len(username) < 3 or len(password) < 4:
            self._json(400, {"ok": False, "error": "username min 3, password min 4"})
            return
        if load_member(username):
            self._json(400, {"ok": False, "error": "username sudah dipakai"})
            return
        create_member(username, password, display, self._ip())
        self._reg_notify(username, display)
        m = load_member(username)
        self._json(200, {"ok": True, "username": username,
                         "status": member_status(m),
                         "msg": f"Berhasil daftar, {username}.",
                         "pay_link": _pay_tg_link(cfg, m)})

    def _api_login(self, data):
        cfg = load_config()
        if not self._rate_limit(f"login:{self._ip()}", cfg.get("rate_max_attempts", 8),
                                cfg.get("rate_window_sec", 600)):
            self._flag("login", "brute-force login (rate-limit)")
            self._json(429, {"ok": False, "error": "terlalu banyak percobaan, coba lagi nanti"})
            return
        username = str(data.get("username") or "").strip()
        password = str(data.get("password") or "")
        m = load_member(username)
        ok = False
        if m and m.get("password"):
            try:
                ok = secrets.compare_digest(dec_secret(m["password"]), password)
            except Exception:  # noqa: BLE001
                ok = False
        log_login(username, ok, self._ip())
        track.login(self._ip(), username, ok)
        if not ok:
            self._flag("login", "brute-force login (kredensial salah)")
            self._json(401, {"ok": False, "error": "username/password salah"})
            return
        st = member_status(m)
        if st == "banned":
            self._json(403, {"ok": False, "status": st, "error": "akun diblokir"})
            return
        if st == "pending":
            self._json(403, {"ok": False, "status": st, "error": "menunggu konfirmasi pembayaran",
                             "pay_link": _pay_tg_link(cfg, m)})
            return
        if st == "expired":
            self._json(403, {"ok": False, "status": st, "error": "langganan kedaluwarsa — hubungi owner",
                             "pay_link": _pay_tg_link(cfg, m)})
            return
        tok = issue_member_session(username, self._ip())
        self._login_notify(username)
        self._json(200, {"ok": True, "token": tok, "username": username,
                         "status": st, "expires_at": m.get("expires_at"),
                         "chat_url": "/chat"})

    def _api_ping(self, data):
        """Beacon browser: perbarui detail perangkat visitor (screen, tz, dll)."""
        if isinstance(data, dict):
            info = {k: data.get(k) for k in
                    ("screen", "tz", "lang", "cpu_cores", "mem_gb", "battery")
                    if data.get(k) is not None}
            track.ping(self._ip(), info)
        self._json(200, {"ok": True})

    def _api_status(self, m):
        if not m:
            self._json(401, {"ok": False, "error": "login dulu"})
            return
        self._json(200, {"ok": True, "username": m["username"],
                         "status": member_status(m),
                         "expires_at": m.get("expires_at")})

    def _api_me(self, m):
        if not m:
            self._json(401, {"ok": False, "error": "login dulu"})
            return
        self._json(200, {"ok": True, "username": m["username"],
                         "display_name": m.get("display_name"),
                         "role": m.get("role", "member"),
                         "status": member_status(m),
                         "expires_at": m.get("expires_at"),
                         "pay_link": _pay_tg_link(load_config(), m)})

    # --- handlers ---
    def _serve_qr(self, cfg):
        qr = _qr_source(cfg)
        if not qr:
            self._send(404, "no qr")
            return
        ctype = mimetypes.guess_type(qr)[0] or "image/jpeg"
        self._send(200, Path(qr).read_bytes(), ctype,
                   headers={"Cache-Control": "public, max-age=3600"})

    def _post_login(self, cfg, data):
        if not self._rate_limit(f"login:{self._ip()}", cfg.get("rate_max_attempts", 8),
                                cfg.get("rate_window_sec", 600)):
            self._flag("login", "brute-force login (rate-limit)")
            self._html(_login_page(cfg, err="terlalu banyak percobaan, coba lagi nanti").encode())
            return
        username = str(data.get("username") or "").strip()
        password = str(data.get("password") or "")
        m = load_member(username)
        ok = False
        if m and m.get("password"):
            try:
                stored = dec_secret(m["password"])
                ok = secrets.compare_digest(stored, password)
            except Exception:  # noqa: BLE001
                ok = False
        log_login(username, ok, self._ip())
        track.login(self._ip(), username, ok)
        if not ok:
            self._flag("login", "brute-force login (kredensial salah)")
            self._html(_login_page(cfg, err="username/password salah").encode())
            return
        st = member_status(m)
        if st == "banned":
            self._html(_login_page(cfg, err="akun diblokir (banned)").encode())
            return
        if st == "pending":
            self._html(_login_page(cfg, err="menunggu konfirmasi pembayaran").encode())
            return
        if st == "expired":
            self._html(_login_page(cfg, err="langganan kedaluwarsa — hubungi owner").encode())
            return
        tok = issue_member_session(username, self._ip())
        self._login_notify(username)
        self._redirect("/chat", {"Set-Cookie": _cookie(
            "denz_member", tok, secure=getattr(self, "_secure", False))})

    def _post_register(self, cfg, data):
        if not self._rate_limit(f"reg:{self._ip()}", cfg.get("rate_max_attempts", 8),
                                cfg.get("rate_window_sec", 600)):
            self._flag("register", "spam registrasi (rate-limit)")
            self._html(_register_page(cfg, err="terlalu banyak percobaan, coba lagi nanti").encode())
            return
        username = str(data.get("username") or "").strip()
        display = str(data.get("display_name") or "").strip()
        password = str(data.get("password") or "")
        if len(username) < 3 or len(password) < 4:
            self._html(_register_page(cfg, err="username min 3, password min 4").encode())
            return
        if load_member(username):
            self._html(_register_page(cfg, err="username sudah dipakai").encode())
            return
        create_member(username, password, display, self._ip())
        self._reg_notify(username, display)
        m = load_member(username)
        self._html(_register_page(
            cfg, msg=f"Berhasil daftar, {username}.", m=m).encode())

    def _post_admin_add(self, cfg, data):
        """Halaman admin (reseller): tambah member langsung aktif."""
        username = str(data.get("username") or "").strip()
        display = str(data.get("display_name") or "").strip()
        password = str(data.get("password") or "")
        days = str(data.get("days") or "").strip()
        if len(username) < 3 or len(password) < 4:
            self._html(_admin_add_page(cfg, err="username min 3, password min 4").encode())
            return
        if load_member(username):
            self._html(_admin_add_page(cfg, err="username sudah dipakai").encode())
            return
        try:
            days_i = int(days) if days else None
        except ValueError:
            days_i = None
        add_member_active(username, password, display, days=days_i,
                          role="member", by=self._ip())
        from denzbot import tg_notify
        tg_notify(f"➕ MEMBER BARU via admin web: {username} ({display})")
        m = load_member(username)
        self._html(_admin_add_page(
            cfg, msg=f"✅ Member {username} aktif s/d {m.get('expires_at')}.").encode())

    def _post_chat(self, data):
        m, _ = self._auth_member()
        if not m:
            self._json(401, {"error": "login dulu"})
            return
        st = member_status(m)
        if st != "active":
            self._json(403, {"error": f"status: {st}"})
            return
        cfg = load_config()
        if not self._rate_limit(f"chat:{m['username']}",
                                cfg.get("chat_rate_max", 30),
                                cfg.get("chat_rate_window", 60)):
            self._json(429, {"error": "terlalu cepat, tunggu sebentar"})
            return
        prompt = str(data.get("message") or "").strip()
        if not prompt:
            self._json(400, {"error": "pesan kosong"})
            return
        reply, error = member_chat(m["username"], prompt)
        if error:
            self._json(500, {"error": error})
        else:
            self._json(200, {"reply": reply or "(tanpa jawaban)"})

    def _post_chat_stream(self, data):
        """Chat streaming: kirim NDJSON chunked per potongan jawaban AI."""
        m, _ = self._auth_member()
        if not m:
            self._json(401, {"error": "login dulu"})
            return
        st = member_status(m)
        if st != "active":
            self._json(403, {"error": f"status: {st}"})
            return
        cfg = load_config()
        if not self._rate_limit(f"chat:{m['username']}",
                                cfg.get("chat_rate_max", 30),
                                cfg.get("chat_rate_window", 60)):
            self._json(429, {"error": "terlalu cepat, tunggu sebentar"})
            return
        prompt = str(data.get("message") or "").strip()
        if not prompt:
            self._json(400, {"error": "pesan kosong"})
            return
        import denzyx
        state = _chat_state(m["username"])
        q = queue.Queue()
        t = threading.Thread(target=denzyx.stream_chat,
                             args=(state, prompt, q), daemon=True)
        t.start()
        self.send_response(200)
        self.send_header("Content-Type", "application/x-ndjson; charset=utf-8")
        self.send_header("Transfer-Encoding", "chunked")
        self.send_header("Cache-Control", "no-store")
        self.end_headers()

        def emit(obj):
            raw = (json.dumps(obj, ensure_ascii=False) + "\n").encode("utf-8")
            self.wfile.write(f"{len(raw):x}\r\n".encode() + raw + b"\r\n")
            self.wfile.flush()

        try:
            parts, error = [], None
            while True:
                try:
                    kind, val = q.get(timeout=0.5)
                except queue.Empty:
                    if not t.is_alive():
                        break
                    continue
                if kind == "content":
                    parts.append(val)
                    emit({"t": "text", "d": val})
                elif kind == "error":
                    error = val
                    emit({"t": "error", "d": val})
                elif kind == "done":
                    break
            t.join(timeout=5)
            reply = "".join(parts).strip() if parts else None
            append_chat(m["username"], prompt, reply)
            emit({"t": "done", "d": reply or ""})
            self.wfile.write(b"0\r\n\r\n")
            self.wfile.flush()
        except (BrokenPipeError, ConnectionResetError):
            pass

    def _post_password(self, data):
        m, _ = self._auth_member()
        if not m:
            self._redirect("/login")
            return
        old = str(data.get("old_password") or "")
        new = str(data.get("new_password") or "")
        conf = str(data.get("confirm_password") or "")
        try:
            ok_old = secrets.compare_digest(dec_secret(m["password"]), old)
        except Exception:  # noqa: BLE001
            ok_old = False
        if not ok_old:
            self._html(_password_page(err="password lama salah").encode())
            return
        if len(new) < 4:
            self._html(_password_page(err="password baru minimal 4 karakter").encode())
            return
        if new != conf:
            self._html(_password_page(err="konfirmasi password tidak cocok").encode())
            return
        m["password"] = enc_secret(new)
        save_member(m)
        log_activity("change_password", m["username"])
        from denzbot import tg_notify
        tg_notify(f"🔏 Ganti password member: {m['username']} ({self._ip()})")
        self._html(_password_page(msg="Password berhasil diganti.").encode())

    def _post_owner_login(self, cfg, data):
        if not self._rate_limit(f"owner:{self._ip()}", cfg.get("rate_max_attempts", 8),
                                cfg.get("rate_window_sec", 600)):
            self._flag("owner", "brute-force owner login (rate-limit)")
            self._html(_owner_login_page(cfg, err="terlalu banyak percobaan, coba lagi nanti").encode())
            return
        user = str(data.get("username") or "")
        pw = str(data.get("password") or "")
        own = cfg.get("owner") or {}
        ok = (user == own.get("username")
              and verify_password(pw, own.get("salt", ""), own.get("password_hash", "")))
        log_activity("owner_login", f"{user} -> {'ok' if ok else 'gagal'}")
        if not ok:
            self._flag("owner", "brute-force owner login (kredensial salah)")
            self._html(_owner_login_page(cfg, err="kredensial salah").encode())
            return
        tok = issue_owner_token()
        self._redirect("/owner", {"Set-Cookie": _cookie(
            "denz_owner", tok, secure=getattr(self, "_secure", False))})

    # --- owner ---
    def _owner_get(self, path, cfg):
        q = self._query()
        if path == "/owner" or path == "/owner/":
            self._html(_owner_page(cfg, q=q.get("q", ""),
                                   pg=int(q.get("pg") or 1)).encode())
        elif path == "/owner/logs":
            self._html(_owner_logs_page(q=q.get("q", ""),
                                        pg=int(q.get("pg") or 1)).encode())
        elif path == "/owner/security":
            self._html(_owner_security_page().encode())
        elif path == "/owner/visitors":
            self._html(_owner_visitors_page(
                q=q.get("q", ""), pg=int(q.get("pg") or 1),
                sort=q.get("sort", "last")).encode())
        elif path.startswith("/owner/visitor/"):
            self._html(_owner_visitor_page(
                urllib.parse.unquote(path[len("/owner/visitor/"):])).encode())
        elif path == "/owner/register":
            self._html(_register_page(cfg, msg="Daftarkan member baru dari sini (owner).").encode())
        elif path.startswith("/owner/member/"):
            rest = path[len("/owner/member/"):]
            if rest.endswith("/md"):
                username = re_safe(rest[:-3])
                md = session_md_path(username)
                if md.exists():
                    self._send(200, md.read_text(encoding="utf-8"),
                               "text/plain; charset=utf-8")
                else:
                    self._send(404, "belum ada sesi")
                return
            m = load_member(rest)
            if not m:
                self._send(404, "member tidak ada")
            else:
                self._html(_owner_member_page(m).encode())
        else:
            self._html(page("404", "<p>404</p>").encode())

    def _owner_post(self, path, data):
        if path.startswith("/owner/member/"):
            username = path[len("/owner/member/"):].split("/")[0]
            m = load_member(username)
            if not m:
                self._redirect("/owner")
                return
            action = data.get("action")
            if action == "activate":
                m["status"] = "active"
                m["paid_at"] = _now_iso()
                m["expires_at"] = (datetime.now() + timedelta(days=int(data.get("days") or load_config().get("sub_days", 30)))).isoformat(timespec="seconds")
                log_activity("activate", username)
            elif action == "ban":
                m["status"] = "banned"
                log_activity("ban", username)
            elif action == "unban":
                m["status"] = "active"
                log_activity("unban", username)
            elif action == "extend":
                days = int(data.get("days") or 30)
                base = _parse_dt(m.get("expires_at"))
                if base < datetime.now():
                    base = datetime.now()
                m["expires_at"] = (base + timedelta(days=days)).isoformat(timespec="seconds")
                m["status"] = "active"
                log_activity("extend", f"{username} +{days} hari")
            elif action == "makeadmin":
                m["role"] = "admin"
                log_activity("addadmin", username)
            elif action == "demote":
                m["role"] = "member"
                log_activity("rmadmin", username)
            elif action == "resetpass":
                new = str(data.get("new_password") or "")
                if len(new) >= 4:
                    m["password"] = enc_secret(new)
                    log_activity("resetpass", username)
                else:
                    self._redirect(f"/owner/member/{username}")
                    return
            elif action == "delete":
                delete_member(username)
                try:
                    session_md_path(username).unlink()
                except OSError:
                    pass
                log_activity("delete", username)
                from denzbot import tg_notify
                tg_notify(f"🗑️ Owner hapus pengguna: {username}")
                self._redirect("/owner")
                return
            save_member(m)
            write_session_md(m)
            from denzbot import tg_notify
            tg_notify(f"🛡️ Owner {action}: {username}")
            self._redirect(f"/owner/member/{username}")
        elif path == "/owner/security":
            ip = str(data.get("ip") or "").strip()
            if data.get("action") == "unban" and waf.unban(ip):
                log_activity("waf_unban", ip)
                from denzbot import tg_notify
                tg_notify(f"🛡️ Owner unban IP: {ip}")
                self._html(_owner_security_page(
                    msg=f"IP {ip} dibuka blokirnya.").encode())
                return
            self._redirect("/owner/security")
        elif path == "/owner/visitors":
            ip = str(data.get("ip") or "").strip()
            action = data.get("action")
            if action == "clear":
                track.clear()
                log_activity("visitors_clear", self._ip())
                from denzbot import tg_notify
                tg_notify("🗑️ Owner hapus semua data pengunjung.")
                self._html(_owner_visitors_page(
                    msg="Semua data pengunjung dihapus.").encode())
                return
            if action == "ban":
                if waf.ban(ip, "manual oleh owner (panel pengunjung)"):
                    log_activity("waf_manual_ban", ip)
                    from denzbot import tg_notify
                    tg_notify(f"⛔ Owner ban IP dari panel pengunjung: {ip}")
                self._redirect("/owner/visitors")
                return
            if action == "unban":
                if waf.unban(ip):
                    log_activity("waf_unban", ip)
                self._redirect(f"/owner/visitor/{urllib.parse.quote(ip)}")
                return
            self._redirect("/owner/visitors")
        else:
            self._redirect("/owner")


def _owner_login_page(cfg, msg="", err=""):
    body = f"""<div class="auth"><div class="card authcard">
<div class="auth-hero"><div class="logo">🛡️</div>
<h3>Owner Panel</h3>
<small>Kredensial owner terenkripsi (salted hash)</small></div>
{_flash(msg, err)}
<form method="post" action="/owner/login">
<input type="hidden" name="_csrf" value="__CSRF__">
<label>Username owner</label>
<input name="username" placeholder="owner username" required autocomplete="username">
<label>Password owner</label>
<input name="password" type="password" placeholder="••••••••" required autocomplete="current-password">
<button type="submit">Masuk Owner →</button></form>
<div class="auth-switch"><a href="/login">← ke Login Member</a></div>
</div></div>"""
    return page("Owner Login", body, subtitle="owner")


# ---------------------------------------------------------------------------
# Entry
# ---------------------------------------------------------------------------

_TLS_CIPHERS = ("ECDHE+AESGCM:ECDHE+CHACHA20:EDH+AESGCM:EDH+CHACHA20:"
                "!aNULL:!eNULL:!NULL:!MD5:!RC4:!DES:!3DES:!CBC:!SHA1:"
                "!EXPORT:!PSK:!SRP:!DSS:!LOW:!CAMELLIA:!SEED:!IDEA")


def _ssl_context(cert, key):
    """SSLContext ter-harden: TLS 1.2+ & hanya cipher kuat (anti vuln
    "Weak Cipher Suites"). Kompresi TLS dimatikan (anti CRIME)."""
    import ssl
    ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    ctx.minimum_version = ssl.TLSVersion.TLSv1_2
    ctx.set_ciphers(_TLS_CIPHERS)
    ctx.options |= ssl.OP_CIPHER_SERVER_PREFERENCE | ssl.OP_NO_COMPRESSION
    ctx.load_cert_chain(cert, key)
    return ctx


def start_server(cfg=None, quiet=False):
    global _HTTPS
    cfg = cfg or load_config()
    _mkdirs()
    host, port = cfg.get("host", "0.0.0.0"), int(cfg.get("port", 8000))
    srv = ThreadingHTTPServer((host, port), Handler)
    cert, key = (cfg.get("ssl_cert") or "").strip(), (cfg.get("ssl_key") or "").strip()
    if cert and key and Path(cert).exists() and Path(key).exists():
        srv.socket = _ssl_context(cert, key).wrap_socket(
            srv.socket, server_side=True)
        _HTTPS = True
        scheme = "https"
    else:
        _HTTPS = False
        scheme = "http"
        if cert or key:
            print("[webdenz] peringatan: ssl_cert/ssl_key diisi tapi file tidak ada — pakai HTTP")
    if not quiet:
        print(f"[webdenz] server jalan di {scheme}://{host}:{port}")
    if host in ("0.0.0.0", "::"):
        print("[webdenz] ⚠️ keamanan: host 0.0.0.0 memaparkan server ke semua "
              "antarmuka. Sebaiknya pakai 127.0.0.1 (akses hanya via tunnel "
              "cloudflared). Ubah di webconfig.json → 'host'.")
    srv.serve_forever()
    return srv


def run_server(cfg=None):
    try:
        import lic
        lic.require()
    except SystemExit:
        raise
    except Exception:  # noqa: BLE001
        pass
    start_server(cfg)


if __name__ == "__main__":
    run_server()
