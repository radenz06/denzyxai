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

Data & rahasia disimpan di webconfig.json (gitignored) dan webdata/
(gitignored). JANGAN commit webconfig.json ke repo publik.
"""

import base64
import hashlib
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

BASE_DIR = Path(__file__).resolve().parent
CONFIG_PATH = Path(os.environ.get("WEBDENZ_CONFIG") or BASE_DIR / "webconfig.json")
DATA_DIR = Path(os.environ.get("WEBDENZ_DATA") or BASE_DIR / "webdata")
MEMBERS_DIR = DATA_DIR / "members"
SESSIONS_DIR = DATA_DIR / "sessions"
LOGS_DIR = DATA_DIR / "logs"
QR_CACHE = DATA_DIR / "qr_cache"

PRICE_DEFAULT = 20000
SUB_DAYS_DEFAULT = 30

# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------

_DEFAULTS = {
    "tg_bot_token": "",
    "tg_chat_id": "",
    "tg_owner_username": "",
    "owner": {"username": "denzyx", "password_hash": "", "salt": ""},
    "secret": "",
    "price_idr": PRICE_DEFAULT,
    "sub_days": SUB_DAYS_DEFAULT,
    "qr_path": "",
    "host": "0.0.0.0",
    "port": 8000,
    "ai": {"model": "deepseek-v4-flash-free", "max_tokens": 1024},
}


def load_config():
    cfg = json.loads(json.dumps(_DEFAULTS))
    try:
        data = json.loads(CONFIG_PATH.read_text(encoding="utf-8"))
        for k, v in data.items():
            if isinstance(v, dict) and isinstance(cfg.get(k), dict):
                cfg[k].update(v)
            else:
                cfg[k] = v
    except (OSError, json.JSONDecodeError):
        pass
    if not cfg.get("secret"):
        cfg["secret"] = secrets.token_hex(32)
        save_config(cfg)
    return cfg


def save_config(cfg):
    CONFIG_PATH.parent.mkdir(parents=True, exist_ok=True)
    CONFIG_PATH.write_text(json.dumps(cfg, ensure_ascii=False, indent=2),
                           encoding="utf-8")


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
    return bool(token) and token == cfg.get("owner_token")


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

def member_chat(username, prompt):
    import denzyx
    m = load_member(username)
    if not m:
        return None, "member tidak ditemukan"
    state = denzyx.State()
    state.cwd = Path(BASE_DIR)
    state.model = (load_config().get("ai") or {}).get("model", denzyx.State().model)
    state.max_tokens = (load_config().get("ai") or {}).get("max_tokens", 1024)
    state.messages = list(m.get("messages") or [])
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

_CSS = """
body{font-family:system-ui,sans-serif;background:#0f1115;color:#e6e6e6;
max-width:760px;margin:0 auto;padding:16px}a{color:#6ea8fe}
.card{background:#171a21;border:1px solid #262b36;border-radius:12px;
padding:18px;margin:14px 0}
input,textarea,select,button{background:#0f1115;color:#e6e6e6;
border:1px solid #333;border-radius:8px;padding:10px;font-size:15px;
width:100%;box-sizing:border-box;margin:6px 0}
button{background:#2b6bff;border:none;cursor:pointer;font-weight:600}
button.danger{background:#b3261e}button.ok{background:#188038}
.badge{display:inline-block;padding:2px 10px;border-radius:20px;font-size:12px}
.pending{background:#8a6d00}.active{background:#188038}.banned{background:#b3261e}
.expired{background:#555}.none{background:#333}
pre{white-space:pre-wrap;background:#0d0f14;padding:10px;border-radius:8px}
.msg{white-space:pre-wrap;margin:8px 0;padding:10px;border-radius:10px}
.user{background:#1b2a4a;text-align:right}.ai{background:#1d232b}
.toolbar a{margin-right:12px;text-decoration:none}
small{color:#8a8f98}table{width:100%;border-collapse:collapse}
th,td{text-align:left;padding:6px;border-bottom:1px solid #262b36}
.paycard{background:#123;border:1px solid #2b6bff;border-radius:12px;
padding:16px;margin:14px 0;text-align:center}
a.paybtn{display:inline-block;background:#2b6bff;color:#fff;padding:12px 18px;
border-radius:10px;text-decoration:none;font-weight:700;font-size:16px}
"""

_PAGE = """<!doctype html><html lang="id"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>{title} · denzyx AI</title><style>{css}</style></head>
<body><div style="text-align:center;margin:10px 0">
<b style="font-size:20px">denzyx AI</b> <small>{subtitle}</small></div>
{body}</body></html>"""


def page(title, body, subtitle="member area"):
    return _PAGE.format(title=title, css=_CSS, body=body, subtitle=subtitle)


def _qr_html(cfg):
    qr = cfg.get("qr_path")
    if qr and Path(qr).exists():
        return (f'<p><img src="/qr" alt="QR pembayaran" '
                f'style="max-width:220px;border-radius:10px"></p>')
    return ("<p><small>QR pembayaran belum dipasang — transfer ke kontak owner "
            "lalu sebutkan username saat konfirmasi.</small></p>")


def _login_page(cfg, msg="", err=""):
    body = f"""<div class="card"><h3>Login Member</h3>{_flash(msg, err)}
<form method="post" action="/login">
<input name="username" placeholder="username" required>
<input name="password" type="password" placeholder="password" required>
<button type="submit">Masuk</button></form>
<p><small>Belum daftar? <a href="/register">Registrasi (langganan 1 bulan · Rp {cfg.get('price_idr'):,})</a></small></p></div>"""
    return page("Login", body)


def _register_page(cfg, msg="", err="", m=None):
    pay = ""
    if m and member_status(m) == "pending":
        pay = f"""<div class="paycard"><b>Langkah aktivasi:</b><br>
1. Klik tombol di bawah untuk chat owner di Telegram<br>
2. Kirim bukti & dapatkan QR pembayaran<br>
3. Setelah dibayar, akun kamu diaktifkan otomatis
{_pay_tg_link(cfg, m)}
<p><small>Status kamu: <b>menunggu konfirmasi</b></small></p></div>"""
    body = f"""<div class="card"><h3>Registrasi Member</h3>
 <p><small>Langganan 1 bulan · <b>Rp {cfg.get('price_idr'):,}</b></small></p>
 {pay}
 {_flash(msg, err)}
 <form method="post" action="/register">
 <input name="username" placeholder="username (login)" required>
 <input name="display_name" placeholder="nama panggilan">
 <input name="password" type="password" placeholder="password" required>
 <button type="submit">Daftar & Langganan</button></form>
 <p><small>Sudah daftar? <a href="/login">Login</a></small></p></div>"""
    return page("Registrasi", body)


def _flash(msg, err):
    out = ""
    if msg:
        out += f'<p style="color:#6ea8fe">{msg}</p>'
    if err:
        out += f'<p style="color:#ff6b6b">{err}</p>'
    return out


def _pay_tg_link(cfg, m):
    """Link Telegram DM ke owner dengan pesan permintaan QR (blank payment)."""
    owner = (cfg.get("tg_owner_username") or "").strip().lstrip("@")
    if not owner:
        return ""
    text = ("halo bang denz, ane mau berlanggan denzyx ai, "
            "bisa kirimkan qr sekarang?")
    if m and m.get("username"):
        text += f"\nusername: {m['username']}"
    url = "https://t.me/" + urllib.parse.quote(owner)
    return f'<a class="paybtn" target="_blank" rel="noopener" href="{url}?text={urllib.parse.quote(text)}">💬 Minta QR ke Owner</a>'


def _chat_page(m, cfg):
    msgs_html = ""
    for x in m.get("messages") or []:
        cls = "user" if x.get("role") == "user" else "ai"
        who = "Kamu" if x.get("role") == "user" else "Denzyx"
        body = x.get("content") or ""
        msgs_html += (f'<div class="msg {cls}"><small>{who}</small><br>'
                      f"{body}</div>")
    return page("Chat", f"""
<div class="toolbar"><a href="/chat">Chat</a>
<a href="/status">Status Langganan</a>
<a href="/logout">Logout</a></div>
<div id="msgs">{msgs_html}</div>
<form id="fm"><input id="inp" placeholder="ketik pesan..." autocomplete="off">
<button type="submit">Kirim</button></form>
<script>
const inp=document.getElementById('inp');
const fm=document.getElementById('fm');
const msgs=document.getElementById('msgs');
fm.onsubmit=async e=>{{e.preventDefault();
const v=inp.value.trim();if(!v)return;inp.value='';
msgs.insertAdjacentHTML('beforeend',
 '<div class="msg user"><small>Kamu</small><br>'+v.replace(/</g,'&lt;')+'</div>');
const d=document.createElement('div');
d.className='msg ai';d.innerHTML='<small>Denzyx</small><br><i>...</i>';
msgs.appendChild(d);msgs.scrollTop=msgs.scrollHeight;
const r=await fetch('/api/chat',{{method:'POST',
 headers:{{'Content-Type':'application/json'}},
 body:JSON.stringify({{message:v}})}});
const j=await r.json();
if(j.error){{d.innerHTML='<small>Denzyx</small><br><i>Error:</i> '+j.error;}}
else{{d.innerHTML='<small>Denzyx</small><br>'+j.reply.replace(/</g,'&lt;');}}
msgs.scrollTop=msgs.scrollHeight;}};
</script>""")


def _status_page(m):
    st = member_status(m)
    badge = f'<span class="badge {st}">{st}</span>'
    pay = ""
    if st == "pending":
        cfg = load_config()
        pay = f"""<div class="paycard"><b>Akun belum aktif</b><br>
Chat owner di Telegram untuk minta QR pembayaran:
{_pay_tg_link(cfg, m)}
<p><small>Rp {cfg.get('price_idr'):,} / {cfg.get('sub_days')} hari — setelah dibayar akun otomatis aktif</small></p></div>"""
    return page("Status", f"""<div class="card"><h3>Status Langganan</h3>
{pay}
<p>Username: <b>{m['username']}</b> {badge}</p>
<p>Nama: {m.get('display_name', '-')}</p>
<p>Aktif s/d: <b>{m.get('expires_at') or '-'}</b></p>
<p>Terdaftar: {m.get('created_at')}</p>
<p><small><a href="/chat">← ke Chat</a> · <a href="/logout">Logout</a></small></p></div>""")


def _owner_page(cfg, msg="", err=""):
    rows = []
    for m in list_members():
        st = member_status(m)
        badge = f'<span class="badge {st}">{st}</span>'
        rows.append(f"<tr><td>{m['username']}</td><td>{m.get('display_name','-')}"
                    f"</td><td>{badge}</td><td>{m.get('expires_at') or '-'}</td>"
                    f"<td><a href='/owner/member/{m['username']}'>detail</a></td></tr>")
    table = ("<table><tr><th>username</th><th>nama</th><th>status</th>"
             "<th>aktif s/d</th><th></th></tr>" + "".join(rows) + "</table>")
    return page("Owner Panel", f"""<div class="toolbar">
<a href="/owner">Dashboard</a><a href="/owner/logs">Log</a>
<a href="/owner/register">+ Daftarkan Member</a><a href="/logout">Logout</a></div>
{_flash(msg, err)}
<div class="card"><h3>Owner Panel</h3>
<p>Member: {len(list_members())} · Server: {cfg.get('host')}:{cfg.get('port')}
 · Harga: Rp {cfg.get('price_idr'):,} / {cfg.get('sub_days')} hari</p></div>
<div class="card">{table}</div>""", subtitle="owner")


def _owner_member_page(m):
    st = member_status(m)
    pw = "-"
    try:
        pw = dec_secret(m["password"])
    except Exception:  # noqa: BLE001
        pw = "(tidak bisa didecrypt)"
    act = f"""<form method="post" style="display:inline">
<button class="ok" name="action" value="activate">Aktivasi 30 hari</button></form>
<form method="post" style="display:inline">
<button name="action" value="ban">Ban</button></form>
<form method="post" style="display:inline">
<button class="ok" name="action" value="unban">Unban</button></form>
<form method="post" style="display:inline">
<input name="days" value="30" style="width:70px;display:inline">
<button name="action" value="extend">Perpanjang (hari)</button></form>"""
    return page(f"Member {m['username']}", f"""<div class="card">
<a href="/owner">← Owner Panel</a>
<h3>Member: {m['username']} <span class="badge {st}">{st}</span></h3>
<p>Nama: {m.get('display_name','-')}</p>
<p>Password (encrypt di simpan, ini buat owner): <code>{pw}</code></p>
<p>Aktif s/d: <b>{m.get('expires_at') or '-'}</b></p>
<p>Terdaftar: {m.get('created_at')} · IP: {m.get('ip') or '-'}</p>
<p>Login: {m.get('login_count',0)}x · terakhir {m.get('last_login') or '-'}</p>
<p>Catatan: {m.get('note') or '-'}</p>
{act}</div>
<div class="card"><h4>Sesi aktif</h4><pre>{json.dumps(m.get('sessions') or [], indent=2, ensure_ascii=False)}</pre></div>
<div class="card"><h4>Riwayat chat</h4>
<a href="/owner/member/{m['username']}/md">Lihat file md sesi</a></div>""",
                 subtitle="owner")


def _owner_logs_page():
    reg = "<br>".join(html_esc(x) for x in read_log("register", 100)) or "-"
    login = "<br>".join(html_esc(x) for x in read_log("login", 100)) or "-"
    adm = "<br>".join(html_esc(x) for x in read_log("admin", 150)) or "-"
    return page("Log Owner", f"""<a href="/owner">← Owner Panel</a>
<div class="card"><h3>Registrasi (username + password)</h3><pre>{reg}</pre></div>
<div class="card"><h3>Login</h3><pre>{login}</pre></div>
<div class="card"><h3>Aktivitas admin</h3><pre>{adm}</pre></div>""",
                 subtitle="owner")


def html_esc(s):
    import html
    return html.escape(str(s))


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    # --- helpers ---
    def _send(self, code, body=b"", ctype="text/html; charset=utf-8",
              headers=None):
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        for k, v in (headers or {}).items():
            self.send_header(k, v)
        self.end_headers()
        if body:
            self.wfile.write(body)

    def _json(self, code, obj, headers=None):
        self._send(code, json.dumps(obj, ensure_ascii=False).encode(),
                   "application/json; charset=utf-8", headers)

    def _cookies(self):
        c = SimpleCookie()
        raw = self.headers.get("Cookie")
        if raw:
            c.load(raw)
        return {k: m.value for k, m in c.items()}

    def _form(self):
        ln = int(self.headers.get("Content-Length") or 0)
        body = self.rfile.read(ln) if ln else b""
        ctype = self.headers.get("Content-Type", "")
        if "json" in ctype:
            try:
                return json.loads(body.decode("utf-8") or "{}")
            except json.JSONDecodeError:
                return {}
        return {k: v[0] for k, v in
                urllib.parse.parse_qs(body.decode("utf-8")).items()}

    def _redirect(self, loc, headers=None):
        self._send(HTTPStatus.FOUND, b"", headers={"Location": loc,
                                                   **(headers or {})})

    def _auth_member(self):
        tok = self._cookies().get("denz_member")
        if not tok:
            return None, None
        return member_by_token(tok)

    def _auth_owner(self):
        return owner_token_valid(self._cookies().get("denz_owner"))

    # --- routes ---
    def do_GET(self):
        cfg = load_config()
        path = urllib.parse.urlparse(self.path).path
        m, _ = self._auth_member()
        if path == "/":
            self._redirect("/chat" if m else "/login")
        elif path == "/login":
            self._send(200, _login_page(cfg).encode())
        elif path == "/register":
            self._send(200, _register_page(cfg).encode())
        elif path == "/qr":
            self._serve_qr(cfg)
        elif path == "/status":
            if not m:
                self._redirect("/login")
            else:
                self._send(200, _status_page(m).encode())
        elif path == "/chat":
            if not m:
                self._redirect("/login")
            else:
                self._send(200, _chat_page(m, cfg).encode())
        elif path == "/logout":
            self._redirect("/login")
        elif path.startswith("/owner"):
            if not self._auth_owner():
                self._send(200, _owner_login_page(cfg).encode())
            else:
                self._owner_get(path, cfg)
        else:
            self._send(404, page("404", "<p>404</p>").encode())

    def do_POST(self):
        cfg = load_config()
        path = urllib.parse.urlparse(self.path).path
        data = self._form()
        if path == "/login":
            self._post_login(cfg, data)
        elif path == "/register":
            self._post_register(cfg, data)
        elif path == "/api/chat":
            self._post_chat(data)
        elif path == "/owner/login":
            self._post_owner_login(cfg, data)
        elif path.startswith("/owner"):
            if not self._auth_owner():
                self._redirect("/owner")
            else:
                self._owner_post(path, data)
        else:
            self._send(404, page("404", "<p>404</p>").encode())

    # --- handlers ---
    def _serve_qr(self, cfg):
        qr = cfg.get("qr_path")
        if not qr or not Path(qr).exists():
            self._send(404, "no qr")
            return
        ctype = mimetypes.guess_type(qr)[0] or "image/jpeg"
        self._send(200, Path(qr).read_bytes(), ctype)

    def _post_login(self, cfg, data):
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
        log_login(username, ok, self.client_address[0])
        if not ok:
            self._send(200, _login_page(cfg, err="username/password salah").encode())
            return
        st = member_status(m)
        if st == "banned":
            self._send(200, _login_page(cfg, err="akun diblokir (banned)").encode())
            return
        if st == "pending":
            self._send(200, _login_page(cfg, err="menunggu konfirmasi pembayaran").encode())
            return
        if st == "expired":
            self._send(200, _login_page(cfg, err="langganan kedaluwarsa — hubungi owner").encode())
            return
        tok = issue_member_session(username, self.client_address[0])
        from denzbot import tg_notify
        tg_notify(f"🔓 Login member: {username} ({self.client_address[0]})")
        self._redirect("/chat", {"Set-Cookie": f"denz_member={tok}; Path=/; HttpOnly"})

    def _post_register(self, cfg, data):
        username = str(data.get("username") or "").strip()
        display = str(data.get("display_name") or "").strip()
        password = str(data.get("password") or "")
        if len(username) < 3 or len(password) < 4:
            self._send(200, _register_page(cfg, err="username min 3, password min 4").encode())
            return
        if load_member(username):
            self._send(200, _register_page(cfg, err="username sudah dipakai").encode())
            return
        create_member(username, password, display, self.client_address[0])
        from denzbot import tg_notify
        tg_notify(f"📝 REGISTRASI baru: {username} ({display}) — IP {self.client_address[0]}. Cek owner panel untuk aktivasi.")
        m = load_member(username)
        self._send(200, _register_page(
            cfg, msg=f"Berhasil daftar, {username}.", m=m).encode())

    def _post_chat(self, data):
        m, _ = self._auth_member()
        if not m:
            self._json(401, {"error": "login dulu"})
            return
        st = member_status(m)
        if st != "active":
            self._json(403, {"error": f"status: {st}"})
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

    def _post_owner_login(self, cfg, data):
        user = str(data.get("username") or "")
        pw = str(data.get("password") or "")
        own = cfg.get("owner") or {}
        ok = (user == own.get("username")
              and verify_password(pw, own.get("salt", ""), own.get("password_hash", "")))
        log_activity("owner_login", f"{user} -> {'ok' if ok else 'gagal'}")
        if not ok:
            self._send(200, _owner_login_page(cfg, err="kredensial salah").encode())
            return
        tok = issue_owner_token()
        self._redirect("/owner", {"Set-Cookie": f"denz_owner={tok}; Path=/; HttpOnly"})

    # --- owner ---
    def _owner_get(self, path, cfg):
        if path == "/owner" or path == "/owner/":
            self._send(200, _owner_page(cfg).encode())
        elif path == "/owner/logs":
            self._send(200, _owner_logs_page().encode())
        elif path == "/owner/register":
            self._send(200, _register_page(cfg, msg="Daftarkan member baru dari sini (owner).").encode())
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
                self._send(200, _owner_member_page(m).encode())
        else:
            self._send(404, page("404", "<p>404</p>").encode())

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
            save_member(m)
            write_session_md(m)
            from denzbot import tg_notify
            tg_notify(f"🛡️ Owner {action}: {username}")
            self._redirect(f"/owner/member/{username}")
        else:
            self._redirect("/owner")


def _owner_login_page(cfg, msg="", err=""):
    return page("Owner Login", f"""<div class="card"><h3>Owner Panel</h3>
{_flash(msg, err)}
<form method="post" action="/owner/login">
<input name="username" placeholder="owner username" required>
<input name="password" type="password" placeholder="owner password" required>
<button type="submit">Masuk Owner</button></form>
<p><small>Kredensial owner terenkripsi (salted hash).</small></p></div>""",
                 subtitle="owner")


# ---------------------------------------------------------------------------
# Entry
# ---------------------------------------------------------------------------

def start_server(cfg=None, quiet=False):
    cfg = cfg or load_config()
    _mkdirs()
    host, port = cfg.get("host", "0.0.0.0"), int(cfg.get("port", 8000))
    srv = ThreadingHTTPServer((host, port), Handler)
    if not quiet:
        print(f"[webdenz] server jalan di http://{host}:{port}")
    srv.serve_forever()
    return srv


def run_server(cfg=None):
    start_server(cfg)


if __name__ == "__main__":
    run_server()
