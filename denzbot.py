#!/usr/bin/env python3
"""denzbot — bot Telegram untuk webdenz (member & admin).

- Semua event (registrasi, request, login, ban, dsb) diteruskan ke chat owner.
- Owner (chat id sesuai webconfig.json) bisa kontrol lewat bot:
  /start /status /members /member <user> /ban <user> /unban <user>
  /activate <user> /approve <user> /reject <user> /extend <user> <hari>
  /addmember <user> <pass> [hari] /addadmin <user> /rmadmin <user> /logs
- Admin (reseller) hanya bisa /addmember.
- Daftar 2 metode: registrasi di web, ATAU request langsung via bot
  (/daftar <username> <password>) → owner approve langsung dari bot.
- Tanpa dependency eksternal: pakai Bot API via urllib (long polling).
"""

import json
import re
import sys
import time
import uuid
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


def tg_send_photo(chat_id, photo, caption="", token=None):
    """Kirim foto (QR pembayaran) via multipart/form-data — tanpa dependency."""
    cfg = webdenz.load_config()
    token = token or cfg.get("tg_bot_token")
    photo = str(photo)
    if not token or not chat_id or not Path(photo).exists():
        return {"ok": False, "description": "photo/token/chat_id tidak valid"}
    boundary = "----denzbot" + uuid.uuid4().hex
    data = Path(photo).read_bytes()
    head = (f"--{boundary}\r\n"
            f'Content-Disposition: form-data; name="chat_id"\r\n\r\n{chat_id}\r\n'
            f"--{boundary}\r\n"
            f'Content-Disposition: form-data; name="caption"\r\n\r\n'
            f"{caption}\r\n"
            f"--{boundary}\r\n"
            f'Content-Disposition: form-data; name="photo"; '
            f'filename="{Path(photo).name}"\r\n'
            f"Content-Type: image/jpeg\r\n\r\n").encode("utf-8")
    tail = ("\r\n" + "--" + boundary + "--\r\n").encode("utf-8")
    url = f"{API}/bot{token}/sendPhoto"
    req = urllib.request.Request(
        url, data=head + data + tail,
        headers={"Content-Type": f"multipart/form-data; boundary={boundary}",
                 "User-Agent": "denzbot"})
    try:
        with urllib.request.urlopen(req, timeout=60) as r:
            return json.loads(r.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        return {"ok": False,
                "description": e.read().decode("utf-8", errors="replace")[:300]}
    except Exception as e:  # noqa: BLE001
        return {"ok": False, "description": str(e)}


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
        role = " 👑ADMIN" if webdenz.is_admin(m) else ""
        lines.append(f"- {m.get('username')}{role} [{st}] s/d {exp}")
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
            f"Role: {m.get('role') or 'member'}\n"
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
    if action == "activate" and m.get("tg_chat_id"):
        tg_send(m["tg_chat_id"],
                f"🎉 Akun {username} sudah AKTIF!\n"
                f"Login di web (username: {username}) dan mulai pakai denzyx AI. "
                f"Langganan s/d {m.get('expires_at')}")
    return f"✅ {action} {username} → {webdenz.member_status(m)}"


def _cmd_addmember(cfg, arg):
    """Tambah member langsung AKTIF (owner & admin)."""
    parts = (arg or "").split()
    if len(parts) < 2:
        return "Pakai: /addmember <username> <password> [hari]"
    username, password = parts[0], parts[1]
    days = int(parts[2]) if len(parts) > 2 and parts[2].isdigit() else None
    if len(username) < 3 or len(password) < 4:
        return "✖ username min 3 karakter, password min 4 karakter."
    if webdenz.load_member(username):
        return f"✖ username {username} sudah dipakai."
    m = webdenz.add_member_active(username, password, days=days,
                                  role="member", by="bot")
    webdenz.log_activity("add", f"{username} via bot")
    tg_notify(f"➕ MEMBER BARU via bot: {username}\n"
              f"Langganan s/d: {m.get('expires_at')}")
    return (f"✅ Member {username} AKTIF s/d {m.get('expires_at')}.\n"
            f"Login pakai username: {username} / password: {password}")


def _cmd_set_role(cfg, arg, admin=True):
    username = (arg or "").strip()
    m = webdenz.load_member(username)
    if not m:
        return f"✖ Member tidak ada: {username}"
    m["role"] = "admin" if admin else "member"
    webdenz.save_member(m)
    webdenz.log_activity("addadmin" if admin else "rmadmin", username)
    if admin and m.get("tg_chat_id"):
        tg_send(m["tg_chat_id"],
                "👑 Kamu sekarang ADMIN (reseller)! Bisa /addmember "
                "<username> <password> untuk menambah member baru.")
    return f"✅ {username} sekarang {'ADMIN (reseller)' if admin else 'member biasa'}."


def _cmd_reject(cfg, arg):
    username = (arg or "").strip()
    m = webdenz.load_member(username)
    if not m:
        return f"✖ Member tidak ada: {username}"
    if webdenz.member_status(m) != "pending":
        return f"✖ {username} bukan pending (sekarang: {webdenz.member_status(m)})."
    webdenz.delete_member(username)
    webdenz.log_activity("reject", username)
    return f"🗑 Request {username} ditolak & dihapus."


_HANDLERS = {
    "/status": _cmd_status,
    "/members": _cmd_members,
    "/member": _cmd_member,
    "/logs": _cmd_logs,
    "/ban": lambda c, a: _cmd_set_member("ban", a),
    "/unban": lambda c, a: _cmd_set_member("unban", a),
    "/activate": lambda c, a: _cmd_set_member("activate", a),
    "/approve": lambda c, a: _cmd_set_member("activate", a),
    "/reject": _cmd_reject,
    "/addmember": _cmd_addmember,
    "/addadmin": lambda c, a: _cmd_set_role(c, a, True),
    "/rmadmin": lambda c, a: _cmd_set_role(c, a, False),
}


# ---------------------------------------------------------------------------
# Alur member (calon pembeli yang chat bot)
# ---------------------------------------------------------------------------

def _extract_username(text):
    """Ambil username dari pesan: 'username: budi' / 'username budi'."""
    if not text:
        return None
    m = re.search(r"(?:username\s*[:\s=]\s*)(\w+)", text, re.IGNORECASE)
    if m:
        return m.group(1)
    for tok in text.split():
        if tok.startswith("@"):
            return tok[1:]
    return None


def _member_tg(chat_id):
    """Cari member yang chat_id TG-nya sudah tersimpan."""
    for mm in webdenz.list_members():
        if str(mm.get("tg_chat_id") or "") == str(chat_id):
            return mm
    return None


def _save_tg(chat_id):
    """Simpan chat_id TG ke member terakhir yang chat (kalau dikenali)."""
    if not chat_id:
        return None
    m = _member_tg(chat_id)
    if m:
        return m
    return None


def _send_qr(m, chat_id):
    """Kirim QR pembayaran + panduan ke chat member (via URL atau file)."""
    cfg = webdenz.load_config()
    qr = webdenz._qr_source(cfg)
    if not qr:
        return ("QR pembayaran belum dipasang di server. "
                "Hubungi owner: @" + (cfg.get("tg_owner_username") or "admin"))
    caption = (f"Halo {m.get('display_name') or m.get('username')} 👋\n"
               f"Untuk langganan denzyx AI:\n"
               f"• Harga: Rp {cfg.get('price_idr'):,} / {cfg.get('sub_days')} hari\n"
               f"• Transfer ke QR di atas, lalu kirim bukti ke bot ini.\n"
               f"• Setelah dikonfirmasi owner, akun otomatis aktif.\n"
               f"username: {m.get('username')}")
    if qr.startswith(("http://", "https://")):
        # Telegram sendPhoto menerima URL gambar langsung.
        r = tg_api(cfg.get("tg_bot_token"), "sendPhoto",
                   {"chat_id": chat_id, "photo": qr, "caption": caption})
        if r.get("ok"):
            return ("✅ QR pembayaran terkirim. Setelah transfer, "
                    "kirim foto bukti ke bot ini, ya.")
        return "Gagal kirim QR: " + str(r.get("description"))[:120]
    r = tg_send_photo(chat_id, qr, caption)
    if r.get("ok"):
        return ("✅ QR pembayaran terkirim. Setelah transfer, "
                "kirim foto bukti ke bot ini, ya.")
    return "Gagal kirim QR: " + str(r.get("description"))[:120]


def handle_member_message(chat_id, text="", has_photo=False, caption=""):
    """Tangani pesan dari member/pembeli (bukan owner).

    Alur: chat bot → bot kirim QR → kirim bukti → owner konfirmasi → aktif.
    """
    cfg = webdenz.load_config()
    m = _member_tg(chat_id)
    if not m:
        uname = _extract_username(text or caption)
        if uname:
            m = webdenz.load_member(uname)
        else:
            return ("Halo! Untuk langganan denzyx AI, kirim username kamu:\n"
                    "username: <username-nya>\n\n"
                    "(username = yang kamu pakai saat daftar di web)")

    if not m:
        return "Username tidak ditemukan. Sudah daftar di web dulu, ya? 😊"

    m["tg_chat_id"] = str(chat_id)
    webdenz.save_member(m)

    st = webdenz.member_status(m)
    if st == "banned":
        return f"⛔ Akun {m.get('username')} diblokir. Hubungi owner."
    if st == "active":
        return (f"✅ Akun {m.get('username')} sudah AKTIF.\n"
                f"Login di web: /login (username: {m.get('username')})")

    if has_photo:
        webdenz.log_activity("bukti", m.get("username"))
        tg_notify(f"🧾 BUKTI PEMBAYARAN dari {m.get('username')} "
                  f"({m.get('display_name')})\n"
                  f"Chat id: {chat_id}\n"
                  f"Reply: /activate {m.get('username')}")
        return ("📥 Bukti terkirim ke owner. "
                "Begitu dikonfirmasi, akun kamu langsung aktif. "
                "Nanti kamu terima notifikasi di sini 👍")

    if st == "expired":
        return ("⏰ Langganan sudah kedaluwarsa. "
                "Transfer ulang via QR berikut untuk perpanjang:")
    return _send_qr(m, chat_id)


_ADMIN_HELP = ("👑 Panel ADMIN (reseller)\n"
               "Kamu bisa menambahkan member baru:\n"
               "/addmember <username> <password> [hari]\n\n"
               "Contoh: /addmember budi rahasia123\n\n"
               "Perintah lain (ban/extend/approve, dsb) khusus owner.")


def handle_admin_message(chat_id, text=""):
    """Pesan dari admin (reseller) — hanya boleh menambah member."""
    cfg = webdenz.load_config()
    parts = (text or "").split()
    cmd = parts[0].split("@")[0].lower() if parts else ""
    arg = " ".join(parts[1:])
    if cmd in ("/start", "/help"):
        return _ADMIN_HELP
    if cmd == "/addmember":
        return _cmd_addmember(cfg, arg)
    if cmd in ("/status", "/members", "/member", "/logs", "/ban", "/unban",
               "/activate", "/approve", "/extend", "/addadmin", "/rmadmin",
               "/reject"):
        return "⛔ Perintah itu khusus owner. Sebagai admin kamu cuma bisa /addmember."
    return _ADMIN_HELP


def handle_stranger(chat_id, text="", has_photo=False):
    """Calon member yang belum punya akun — bisa request daftar dari bot."""
    cfg = webdenz.load_config()
    parts = (text or "").split()
    cmd = parts[0].split("@")[0].lower() if parts else ""
    if cmd == "/start":
        return ("Halo! Ini bot langganan denzyx AI 🤖\n\n"
                "Daftar langsung dari sini:\n"
                "/daftar <username> <password>\n"
                "Contoh: /daftar budi rahasia123\n\n"
                "Setelah kamu daftar, owner mengonfirmasi, dan akun kamu aktif. "
                "Sudah punya akun? Kirim username kamu untuk dapat QR pembayaran.")
    if cmd == "/daftar":
        args = parts[1:]
        if len(args) < 2:
            return ("Pakai: /daftar <username> <password>\n"
                    "Contoh: /daftar budi rahasia123")
        username, password = args[0], args[1]
        if len(username) < 3 or len(password) < 4:
            return "✖ username min 3 karakter, password min 4 karakter."
        if webdenz.load_member(username):
            return f"✖ username {username} sudah dipakai."
        m = webdenz.create_member(username, password, username, ip="")
        m["tg_chat_id"] = str(chat_id)
        webdenz.save_member(m)
        webdenz.log_activity("request", username)
        tg_notify(f"🙋 REQUEST BARU via bot: {username}\n"
                  f"Chat id: {chat_id}\n"
                  f"Approve: /approve {username}\n"
                  f"Tolak: /reject {username}")
        return (f"📝 Request kamu diterima, {username}!\n"
                f"Owner akan mengonfirmasi sebentar lagi. "
                f"Begitu aktif, kamu dapat notifikasi di sini ✅")
    if has_photo:
        return ("Halo! Kalau mau langganan, daftar dulu:\n"
                "/daftar <username> <password>\n"
                "Contoh: /daftar budi rahasia123")
    # coba kenali akun lama via username ("username: x" / "@x")
    uname = _extract_username(text)
    if uname:
        m = webdenz.load_member(uname)
        if m:
            m["tg_chat_id"] = str(chat_id)
            webdenz.save_member(m)
            return handle_member_message(chat_id, text, has_photo)
    return ("Halo! Untuk daftar langsung dari bot:\n"
            "/daftar <username> <password>\n"
            "Contoh: /daftar budi rahasia123\n\n"
            "Sudah punya akun? Kirim username kamu untuk dapat QR pembayaran.")


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
                "/activate <user> /approve <user> /reject <user> "
                "/ban <user> /unban <user> /extend <user> <hari> "
                "/addmember <user> <pass> [hari] "
                "/addadmin <user> /rmadmin <user>")
    try:
        if arg:
            return fn(cfg, arg)
        return fn(cfg)
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
            has_photo = bool(msg.get("photo"))
            caption = msg.get("caption") or ""
            if chat_id == owner_id:
                if not text:
                    continue
                reply = handle_message(text)
                tg_send(owner_id, reply)
                continue
            # --- member / admin (reseller) / calon member ---
            sender = _member_tg(chat_id)
            if sender and webdenz.is_admin(sender):
                reply = handle_admin_message(chat_id, text)
                tg_send(chat_id, reply)
                continue
            if sender:
                reply = handle_member_message(chat_id, text, has_photo, caption)
                tg_send(chat_id, reply)
                if has_photo:
                    # forward foto bukti ke owner
                    photo = msg["photo"][-1]["file_id"]
                    tg_api(cfg.get("tg_bot_token"), "sendPhoto",
                           {"chat_id": owner_id, "photo": photo,
                            "caption": f"🧾 Bukti pembayaran dari {sender.get('username')}"})
                continue
            # calon member (belum punya akun) → alur request daftar
            reply = handle_stranger(chat_id, text, has_photo)
            tg_send(chat_id, reply)


if __name__ == "__main__":
    try:
        import lic
        lic.require()
    except SystemExit:
        raise
    except Exception:  # noqa: BLE001
        pass
    run_bot()
