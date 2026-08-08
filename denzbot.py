#!/usr/bin/env python3
"""denzbot — bot Telegram untuk webdenz (member & admin).

- Semua event (registrasi, login, ban, dsb) diteruskan ke chat owner.
- Owner (chat id sesuai webconfig.json) bisa kontrol lewat bot:
  /start /status /members /member <user> /ban <user> /unban <user>
  /activate <user> /extend <user> <hari> /logs /reload
- Tanpa dependency eksternal: pakai Bot API via urllib (long polling).
"""

import json
import sys
import time
import urllib.request
import urllib.parse
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import webdenz  # noqa: E402

API = "https://api.telegram.org"


def tg_api(token, method, payload=None, timeout=20):
    url = f"{API}/bot{token}/{method}"
    data = json.dumps(payload or {}).encode("utf-8")
    req = urllib.request.Request(
        url, data=data, headers={"Content-Type": "application/json",
                                 "User-Agent": "denzbot"})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return json.loads(r.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        body = e.read().decode("utf-8", errors="replace")
        return {"ok": False, "description": body[:200]}
    except Exception as e:  # noqa: BLE001
        return {"ok": False, "description": str(e)}


def tg_send(chat_id, text, token=None):
    cfg = webdenz.load_config()
    token = token or cfg.get("tg_bot_token")
    if not token or not chat_id:
        return {"ok": False, "description": "token/chat_id belum dikonfigurasi"}
    return tg_api(token, "sendMessage",
                  {"chat_id": str(chat_id), "text": str(text)[:4000],
                   "disable_web_page_preview": True})


def tg_notify(text):
    """Kirim notifikasi ke chat owner (no-op kalau belum dikonfigurasi)."""
    cfg = webdenz.load_config()
    if cfg.get("tg_bot_token") and cfg.get("tg_chat_id"):
        try:
            return tg_send(cfg["tg_chat_id"], text)
        except Exception:  # noqa: BLE001
            return {"ok": False, "description": "tg_notify error"}
    return {"ok": False, "description": "not configured"}


# ---------------------------------------------------------------------------
# Perintah owner
# ---------------------------------------------------------------------------

def _member_badge(m):
    st = webdenz.member_status(m)
    return st


def _cmd_status(cfg):
    members = webdenz.list_members()
    active = sum(1 for m in members if webdenz.member_status(m) == "active")
    pending = sum(1 for m in members if webdenz.member_status(m) == "pending")
    banned = sum(1 for m in members if webdenz.member_status(m) == "banned")
    return (f"📊 Status denzyx web\n"
            f"Server: {cfg.get('host')}:{cfg.get('port')}\n"
            f"Member: {len(members)} · aktif {active} · pending {pending} · "
            f"banned {banned}\n"
            f"Harga: Rp {cfg.get('price_idr'):,} / {cfg.get('sub_days')} hari\n"
            f"Owner: {cfg.get('owner', {}).get('username')}")


def _cmd_members(_cfg):
    members = webdenz.list_members()
    if not members:
        return "Belum ada member."
    lines = ["👥 Member:"]
    for m in sorted(members, key=lambda x: x.get("username", "")):
        st = webdenz.member_status(m)
        exp = (m.get("expires_at") or "-")[:10]
        lines.append(f"- {m.get('username')} [{st}] s/d {exp}")
    return "\n".join(lines)


def _cmd_member(_cfg, arg):
    m = webdenz.load_member(arg)
    if not m:
        return f"Member tidak ada: {arg}"
    try:
        pw = webdenz.dec_secret(m["password"])
    except Exception:  # noqa: BLE001
        pw = "(gagal decrypt)"
    return (f"👤 {m.get('username')} [{webdenz.member_status(m)}]\n"
            f"Nama: {m.get('display_name')}\n"
            f"Password: {pw}\n"
            f"Aktif s/d: {m.get('expires_at') or '-'}\n"
            f"IP: {m.get('ip') or '-'}\n"
            f"Login: {m.get('login_count', 0)}x · {m.get('last_login') or '-'}")


def _cmd_logs(_cfg, n=12):
    rows = webdenz.read_log("register", n)
    if not rows:
        return "Log registrasi kosong."
    return "📋 Registrasi terakhir:\n" + "\n".join(
        f"- {r}" for r in rows)


def _cmd_set_member(action, arg, days=None):
    username = (arg or "").strip()
    m = webdenz.load_member(username)
    if not m:
        return f"Member tidak ada: {username}"
    if action == "ban":
        m["status"] = "banned"
    elif action == "unban":
        m["status"] = "active"
    elif action == "activate":
        m["status"] = "active"
        m["paid_at"] = webdenz._now_iso()
        m["expires_at"] = (webdenz.datetime.now() +
                           webdenz.timedelta(days=int(days or webdenz.load_config()
                                                      .get("sub_days", 30)))).isoformat(timespec="seconds")
    elif action == "extend":
        base = webdenz._parse_dt(m.get("expires_at"))
        if base < webdenz.datetime.now():
            base = webdenz.datetime.now()
        m["expires_at"] = (base + webdenz.timedelta(
            days=int(days or 30))).isoformat(timespec="seconds")
        m["status"] = "active"
    webdenz.save_member(m)
    webdenz.write_session_md(m)
    webdenz.log_activity(action, username)
    return f"✅ {action} {username} → {webdenz.member_status(m)}"


_HANDLERS = {
    "/status": _cmd_status,
    "/members": _cmd_members,
    "/member": _cmd_member,
    "/logs": _cmd_logs,
    "/ban": lambda c, a: _cmd_set_member("ban", a),
    "/unban": lambda c, a: _cmd_set_member("unban", a),
    "/activate": lambda c, a: _cmd_set_member("activate", a),
}


def handle_message(text):
    cfg = webdenz.load_config()
    parts = (text or "").split()
    cmd = parts[0].split("@")[0].lower() if parts else ""
    arg = " ".join(parts[1:])
    if cmd == "/start":
        return _cmd_status(cfg)
    if cmd == "/extend":
        args = arg.split()
        if len(args) < 2:
            return "Pakai: /extend <username> <hari>"
        return _cmd_set_member("extend", args[0], args[1])
    fn = _HANDLERS.get(cmd)
    if not fn:
        return ("Perintah owner: /status /members /member <user> /logs "
                "/activate <user> /ban <user> /unban <user> "
                "/extend <user> <hari>")
    try:
        return fn(cfg, arg)
    except Exception as e:  # noqa: BLE001
        return f"error: {e}"


def poll_once(token, offset):
    r = tg_api(token, "getUpdates",
               {"offset": offset, "timeout": 25})
    if not r.get("ok"):
        return offset, None
    upds = r.get("result") or []
    new_offset = offset
    for u in upds:
        new_offset = u.get("update_id", offset) + 1
    return new_offset, upds


def run_bot(stop_evt=None, verbose=True):
    cfg = webdenz.load_config()
    token = cfg.get("tg_bot_token")
    owner_id = str(cfg.get("tg_chat_id") or "")
    if not token:
        print("[denzbot] tg_bot_token kosong — konfigurasi dulu (admin-denz.py setup)")
        return
    if verbose:
        print(f"[denzbot] bot aktif (owner chat id: {owner_id})")
    offset = 0
    while True:
        if stop_evt is not None and stop_evt.is_set():
            break
        try:
            offset, upds = poll_once(token, offset)
        except Exception as e:  # noqa: BLE001
            if verbose:
                print(f"[denzbot] polling error: {e}")
            time.sleep(5)
            continue
        if not upds:
            continue
        for u in upds:
            msg = u.get("message") or {}
            chat = msg.get("chat") or {}
            chat_id = str(chat.get("id") or "")
            text = msg.get("text") or ""
            if not text:
                continue
            if chat_id != owner_id:
                tg_send(chat_id,
                        "Bot ini khusus admin. Kamu bukan admin.")
                continue
            reply = handle_message(text)
            tg_send(owner_id, reply)


if __name__ == "__main__":
    run_bot()
