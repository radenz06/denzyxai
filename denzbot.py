#!/usr/bin/env python3
"""denzbot — bot Telegram untuk webdenz (member & admin).

- Semua event (registrasi, request, login, ban, dll) diteruskan ke chat owner.
- Owner (chat id sesuai webconfig.json) bisa kontrol lewat bot:
  /start /status /members /member <user> /ban <user> /unban <user>
  /activate <user> /approve <user> /reject <user> /extend <user> <hari>
  /addmember <user> <pass> [hari] /addadmin <user> /rmadmin <user> /logs
  /bans /unbanip <ip> /block <ip>  (keamanan WAF)
- Admin (reseller) hanya bisa /addmember.
- Daftar 2 metode: registrasi di web, ATAU request langsung via bot
  (/daftar <username> <password>) → owner approve langsung dari bot.
- Tanpa dependency eksternal: pakai Bot API via urllib (long polling).
"""

import json
import hashlib
import re
import sys
import time
import uuid
import urllib.request
import urllib.parse
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import webdenz  # noqa: E402
import waf  # noqa: E402

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


def tg_send(chat_id, text, token=None, reply_markup=None):
    cfg = webdenz.load_config()
    token = token or cfg.get("tg_bot_token")
    if not token or not chat_id:
        return {"ok": False, "description": "token/chat_id belum dikonfigurasi"}
    payload = {"chat_id": str(chat_id), "text": str(text)[:4000],
               "disable_web_page_preview": True}
    if reply_markup is not None:
        payload["reply_markup"] = reply_markup
    return tg_api(token, "sendMessage", payload)


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


def tg_get_file(token, file_id):
    """Download file dari Telegram (foto bukti) → return bytes atau None."""
    if not token or not file_id:
        return None
    r = tg_api(token, "getFile", {"file_id": file_id})
    if not r.get("ok"):
        return None
    path = (r.get("result") or {}).get("file_path")
    if not path:
        return None
    try:
        url = f"https://api.telegram.org/file/bot{token}/{path}"
        req = urllib.request.Request(url, headers={"User-Agent": "denzbot"})
        with urllib.request.urlopen(req, timeout=30) as resp:
            return resp.read()
    except Exception:  # noqa: BLE001
        return None


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
    price_fmt = f"{cfg.get('price_idr'):,}".replace(",", ".")
    rows = []
    for m in sorted(members, key=lambda x: x.get("username", "")):
        st = webdenz.member_status(m)
        role = " 👑" if webdenz.is_admin(m) else ""
        u = m.get('username')
        exp = m.get('expires_at') or "-"
        try:
            from datetime import datetime, timedelta
            exp_d = datetime.fromisoformat(exp) if exp else None
            now = datetime.now()
            sisa = max(0, (exp_d - now).days) if exp_d else None
            sisa_txt = f"{sisa} hari" if sisa is not None else "tidak terbatas"
        except Exception:
            sisa_txt = "-"
        rows.append(f"🟢 {u} {role} · {st} · s/d {exp} · {sisa_txt}")
    tbl = "\n".join(rows) if rows else "Belum ada member."
    markup = {
        "inline_keyboard": [
            [{"text": "🔄 Refresh", "callback_data": "/status"},
             {"text": "👥 Members", "callback_data": "/members"}],
            [{"text": "➕ Add Member", "callback_data": "/addmember"},
             {"text": "🛡 Keamanan", "callback_data": "/bans"}],
        ]
    }
    return (f"📊 Status denzyx web\n"
            f"Server: {cfg.get('host')}:{cfg.get('port')}\n"
            f"Member: {len(members)} · aktif {active} · pending {pending} · banned {banned}\n"
            f"Harga: Rp {price_fmt} / {cfg.get('sub_days')} hari\n"
            f"Owner: {cfg.get('owner', {}).get('username')}\n\n"
            f"👥 Member list:\n{tbl}", markup)


def _cmd_members(_cfg):
    members = webdenz.list_members()
    if not members:
        return "Belum ada member.\n\n💡 Pakai /addmember <user> <pass> [hari] untuk menambah.", None
    lines = ["👥 Member:"]
    keyrows = []
    for m in sorted(members, key=lambda x: x.get("username", "")):
        st = webdenz.member_status(m)
        exp = (m.get("expires_at") or "-")[:10]
        role = " 👑" if webdenz.is_admin(m) else ""
        u = m.get('username')
        lines.append(f"- {u}{role} [{st}] s/d {exp}")
        # Add inline keyboard row per member for quick actions
        keyrows.append([{"text": f"•{u} {st}", "callback_data": f"/member {u}"}])
    tbl = "\n".join(lines)
    markup = {
        "inline_keyboard": keyrows
        + [
            [{"text": "➕ Add Member", "callback_data": "/addmember"}],
            [{"text": "🔄 Refresh", "callback_data": "/members"}],
        ]
    }
    return (tbl, markup)


def _cmd_member(_cfg, arg):
    m = webdenz.load_member(arg)
    if not m:
        return "✖ Member tidak ada: " + arg, None
    pw = webdenz.dec_secret(m["password"]) if m.get("password") else "(password terenkripsi)"
    st = webdenz.member_status(m)
    exp = m.get('expires_at') or "-"
    role = " 👑" if webdenz.is_admin(m) else ""
    created = m.get('created_at') or "-"
    lastlogin = m.get('last_login') or "-"
    loginc = m.get('login_count', 0)
    text = (f"👤 {m.get('username')}{role}\n"
            f"Status: {st}\n"
            f"Nama: {m.get('display_name') or '-'}\n"
            f"Daftar: {created}\n"
            f"Login: {loginc}x · {lastlogin}\n"
            f"Aktif s/d: {exp}\n"
            f"IP: {m.get('ip') or '-'}\n"
            f"Password: {pw}")
    markup = {
        "inline_keyboard": [
            [{"text": "✏️ Edit", "callback_data": f"/member {m.get('username')}"},
             {"text": "🔒 Ban/Unban", "callback_data": f"/ban {m.get('username')}"}],
            [{"text": "➕ Extend", "callback_data": f"/extend {m.get('username')} 30"},
             {"text": "🔄 Refresh", "callback_data": "/member " + m.get('username')}],
        ]
    }
    return (text, markup)


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


def _cmd_bans(_cfg, arg=""):
    """Daftar IP yang diblokir WAF (owner)."""
    items = waf.list_bans()
    if not items:
        return "✅ Tidak ada IP yang diblokir."
    lines = ["🚫 IP yang DIBLOKIR WAF:"]
    for ip, e in sorted(items.items(),
                        key=lambda kv: kv[1].get("first_seen_ts") or 0,
                        reverse=True)[:20]:
        when = (e.get("last_seen") or "-")[:19]
        lines.append(f"• <code>{ip}</code> — {e.get('reason')} "
                     f"({int(e.get('count') or 1)}x) [{when}]"
                     + (f"\n  📍 {e.get('geo')}" if e.get("geo") else ""))
    lines.append("\nUnban: /unbanip <ip>")
    return "\n".join(lines)


def _cmd_unbanip(_cfg, arg):
    ip = (arg or "").strip()
    if not ip:
        return "Pakai: /unbanip <ip>"
    if not waf.unban(ip):
        return f"✖ IP tidak ada di ban list: {ip}"
    webdenz.log_activity("waf_unban", ip)
    return f"✅ IP {ip} dibuka blokirnya."


def _cmd_block(_cfg, arg):
    ip = (arg or "").strip()
    if not ip:
        return "Pakai: /block <ip> (blokir IP permanen)"
    waf.ban(ip, "manual oleh owner via bot", path="")
    webdenz.log_activity("waf_manual_ban", ip)
    return f"⛔ IP {ip} diblokir permanen. Unban: /unbanip {ip}"


def _menu_keyboard():
    """Inline keyboard menu owner (rapih + tombol)."""
    return {
        "inline_keyboard": [
            [{"text": "📊 Status", "callback_data": "/status"},
             {"text": "👥 Members", "callback_data": "/members"}],
            [{"text": "📋 Logs", "callback_data": "/logs"},
             {"text": "🚫 Bans", "callback_data": "/bans"}],
            [{"text": "➕ Add Member", "callback_data": "/addmember"},
             {"text": "👑 Admin", "callback_data": "/addadmin"}],
            [{"text": "🗂 Menu", "callback_data": "/menu"}],
        ]
    }


def _cmd_menu(cfg):
    """Menu utama owner — tombol inline, rapih & terstruktur."""
    lines = [
        "🗂 MENU OWNER — denzyx AI",
        "",
        "👥 Kelola member:",
        "  /status  /members  /member <user>  /logs",
        "",
        "💰 Langganan:",
        "  /addmember <user> <pass> [hari]",
        "  /activate <user>  (approve bukti)",
        "  /reject <user>  /extend <user> <hari>",
        "  /addadmin <user>  /rmadmin <user>",
        "",
        "🛡 Keamanan:",
        "  /bans  /unbanip <ip>  /block <ip>",
        "  /ban <user>  /unban <user>",
        "",
        "Ketuk tombol di bawah untuk jalan cepat ⬇️",
    ]
    return "\n".join(lines)


_HANDLERS = {
    "/status": _cmd_status,
    "/members": _cmd_members,
    "/member": _cmd_member,
    "/logs": _cmd_logs,
    "/bans": _cmd_bans,
    "/unbanip": _cmd_unbanip,
    "/block": _cmd_block,
    "/menu": _cmd_menu,
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


def _idr(n):
    """Format Rupiah gaya Indonesia: 20000 → '20.000'."""
    try:
        return f"{int(n):,}".replace(",", ".")
    except (TypeError, ValueError):
        return str(n)


def _member_password(m):
    """Password member (decrypt) untuk dikirim ke pembeli — None bila gagal."""
    try:
        return webdenz.dec_secret(m.get("password") or "")
    except Exception:  # noqa: BLE001
        return "(password tidak terbaca, minta ke owner)"


def _verify_payment_proof(username, image_bytes, chat_id, token):
    """OCR + parse nominal bukti pembayaran.

    Logika:
    • Nominal >= harga (price_idr) → aktivasi otomatis + kirim username/password.
    • Nominal kurang → nominal diakumulasi, sisa ditagih ke member
      (mengirim 15.000 dari 20.000 → ditagih 5.000; kirim lagi 5.000 → aktif).
    • Nominal tak terbaca / OCR gagal → terusan ke owner untuk /activate manual.

    Return reply untuk member.
    """
    import payocr
    cfg = webdenz.load_config()
    price = int(cfg.get("price_idr") or 0)
    text, oerr = payocr.ocr_image(image_bytes)
    report = payocr.verify_payment(text, price)
    m = webdenz.load_member(username)
    if not m:
        return "Username tidak ditemukan. Sudah daftar di web dulu, ya? 😊"

    amount = report.get("amount")
    if report["ok"] and webdenz.member_status(m) != "active":
        webdenz.log_activity("activate_auto", username)
        _cmd_set_member("activate", username)
        m = webdenz.load_member(username)
        m["paid"] = 0
        m["last_proof_hash"] = None
        webdenz.save_member(m)
        pw = _member_password(m)
        tg_notify(f"🎉 AUTO-AKTIVASI via OCR bukti:\n"
                  f"• Member: {username}\n"
                  f"• Nominal: Rp {_idr(report['amount'])} "
                  f"(cocok harga Rp {_idr(price)})\n"
                  f"• Baris OCR: {report['line'] or '-'}")
        return (f"✅ Pembayaran berhasil, silahkan login!\n\n"
                f"username: {username}\n"
                f"password: {pw}\n\n"
                f"Langganan aktif s/d {m.get('expires_at')}")
    if report["ok"]:
        return "✅ Bukti terverifikasi. Akun sudah aktif sebelumnya."

    # nominal terbaca tapi kurang → akumulasi + tagih sisa
    if amount is not None and price:
        proof_hash = _proof_hash(text)
        if m.get("last_proof_hash") == proof_hash:
            return (f"⚠️ Bukti yang sama sudah dicatat sebelumnya.\n"
                    f"Sudah terkumpul Rp {_idr(m.get('paid', 0))} "
                    f"dari Rp {_idr(price)}.\n"
                    f"Kurang: Rp {_idr(max(0, price - int(m.get('paid', 0))))}")
        m["paid"] = int(m.get("paid") or 0) + amount
        m["last_proof_hash"] = proof_hash
        webdenz.save_member(m)
        # terkumpul sudah lunas → auto-aktif
        if m["paid"] >= price:
            webdenz.log_activity("activate_auto", username)
            _cmd_set_member("activate", username)
            m = webdenz.load_member(username)
            m["paid"] = 0
            m["last_proof_hash"] = None
            webdenz.save_member(m)
            pw = _member_password(m)
            tg_notify(f"🎉 AUTO-AKTIVASI via OCR (lunas parsial):\n"
                      f"• Member: {username}\n"
                      f"• Total terkumpul: Rp {_idr(price)} (LUNAS)\n"
                      f"• Baris OCR: {report['line'] or '-'}")
            return (f"✅ Pembayaran berhasil, silahkan login!\n\n"
                    f"username: {username}\n"
                    f"password: {pw}\n\n"
                    f"Langganan aktif s/d {m.get('expires_at')}")
        shortage = max(0, price - int(m["paid"]))
        tg_notify(f"💰 TAGIHAN SISA dari {username}\n"
                  f"Chat id: {chat_id}\n"
                  f"• Terbaca: Rp {_idr(amount)}\n"
                  f"• Terkumpul: Rp {_idr(m['paid'])} dari Rp {_idr(price)}\n"
                  f"• Sisa tagihan: Rp {_idr(shortage)}\n"
                  f"• Baris OCR: {report['line'] or '-'}")
        webdenz.log_activity("bukti_parsial", username)
        return (f"💰 Nominal belum lengkap.\n"
                f"• Terbaca: Rp {_idr(amount)}\n"
                f"• Terkumpul: Rp {_idr(m['paid'])}\n"
                f"• Harga: Rp {_idr(price)}\n"
                f"• KURANG: Rp {_idr(shortage)}\n\n"
                f"Transfer sisanya (Rp {_idr(shortage)}) lalu kirim bukti lagi ya 🙏")

    # nominal tak terbaca / OCR gagal → terusan manual ke owner
    ocr_line = report.get("line") or "-"
    det = (f"Rp {_idr(report['amount'])}" if report["amount"] else "tidak terbaca")
    tg_notify(f"🧾 BUKTI PEMBAYARAN dari {username}\n"
              f"Chat id: {chat_id}\n"
              f"• OCR nominal: {det} (harga: Rp {_idr(price)})\n"
              f"• Baris OCR: {ocr_line}\n"
              f"• OCR error: {oerr or '-'}\n"
              f"Reply: /activate {username}")
    webdenz.log_activity("bukti", username)
    return ("📥 Bukti terkirim ke owner. "
            "Begitu dikonfirmasi, akun kamu langsung aktif. "
            "Nanti kamu terima notifikasi di sini 👍")


def _proof_hash(text):
    """Fingerprint teks OCR untuk anti double-credit bukti yang sama."""
    return hashlib.sha256((text or "").strip().encode("utf-8")).hexdigest()


def handle_member_message(chat_id, text="", has_photo=False, caption="", photo_id=None):
    """Tangani pesan dari member/pembeli (bukan owner).

    Alur: chat bot → bot kirim QR → kirim bukti → OCR verifikasi →
    cocok harga → auto-aktivasi; tidak cocok → owner konfirmasi manual.
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
        if photo_id:
            image = tg_get_file(cfg.get("tg_bot_token"), photo_id)
            if image:
                return _verify_payment_proof(m.get("username"), image,
                                             chat_id, cfg.get("tg_bot_token"))
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
               "/reject", "/bans", "/unbanip", "/block"):
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
                "/addadmin <user> /rmadmin <user> "
                "/bans /unbanip <ip> /block <ip>")
    try:
        if arg:
            return fn(cfg, arg)
        return fn(cfg)
    except Exception as e:  # noqa: BLE001
        return f"error: {e}"


def handle_callback(callback_data, owner_id):
    """Respon tombol inline /menu → eksekusi perintah owner."""
    cmd = (callback_data or "").strip()
    if not cmd:
        return None, None
    try:
        reply = handle_message(cmd)
    except Exception as e:  # noqa: BLE001
        reply = f"error: {e}"
    markup = _menu_keyboard() if cmd == "/menu" else None
    return reply, markup


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


def _remind_state():
    """File state pengingat: {username: expires_at_yang_sudah_diingatkan}."""
    p = Path(__file__).resolve().parent / "webdata" / ".remind_expire.json"
    try:
        return json.loads(p.read_text(encoding="utf-8")) if p.exists() else {}
    except (OSError, json.JSONDecodeError):
        return {}


def _remind_save(state):
    try:
        p = Path(__file__).resolve().parent / "webdata" / ".remind_expire.json"
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(json.dumps(state, ensure_ascii=False, indent=2),
                     encoding="utf-8")
    except OSError:
        pass


def _remind_expiring(owner_id, cfg):
    """Ingatkan owner + member yang langganannya hampir habis (H-2 dan H-1).

    Dijalankan berkala dari loop bot. Tidak spam: satu kali per masa
    aktif (state disimpan per expires_at).
    """
    from datetime import datetime, timedelta
    state = _remind_state()
    now = datetime.now()
    due = []          # member yang hampir habis → kirim ke owner
    notif_members = []  # → kirim pengingat langsung ke member (bila ada tg)
    for m in webdenz.list_members():
        if webdenz.member_status(m) != "active":
            continue
        exp = webdenz._parse_dt(m.get("expires_at"))
        if not exp:
            continue
        days = (exp - now).days
        if days not in (1, 2):
            continue
        if state.get(m["username"]) == m.get("expires_at"):
            continue  # sudah diingatkan untuk masa aktif ini
        state[m["username"]] = m.get("expires_at")
        due.append((m, days, exp))
        if m.get("tg_chat_id"):
            notif_members.append((m, days))
    # bersihkan state yang sudah lewat / tidak aktif lagi
    for u in list(state):
        m = webdenz.load_member(u)
        if not m or webdenz.member_status(m) != "active":
            state.pop(u, None)
        else:
            exp = webdenz._parse_dt(m.get("expires_at"))
            if exp and exp < now:
                state.pop(u, None)
    if due or notif_members:
        _remind_save(state)
    # notif ke member langsung
    for m, days in notif_members:
        label = "BESOK" if days == 1 else "2 HARI LAGI"
        text = (f"⏳ Langganan kamu akan habis {label} ({m.get('expires_at')}).\n"
                f"Perpanjang sebelum habis supaya akses tidak terputus.\n"
                f"Hubungi owner untuk perpanjangan 🙏")
        tg_send(m["tg_chat_id"], text)
    # notif ringkas ke owner
    if due:
        lines = [f"⏰ {len(due)} langganan hampir habis:"]
        for m, days, exp in due:
            label = "BESOK" if days == 1 else "H-2"
            lines.append(f"• {m['username']} — {label} ({exp})")
        lines.append("\nPerpanjang: /extend <user> <hari>")
        tg_notify("\n".join(lines))


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
    last_remind = 0.0
    while True:
        if stop_evt is not None and stop_evt.is_set():
            break
        # pengingat langganan hampir habis (tiap 6 jam, non-blocking)
        if time.time() - last_remind >= 6 * 3600:
            last_remind = time.time()
            try:
                _remind_expiring(owner_id, cfg)
            except Exception as e:  # noqa: BLE001
                if verbose:
                    print(f"[denzbot] remind error: {e}")
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
            # --- callback tombol inline (/menu) ---
            cb = u.get("callback_query") or {}
            if cb:
                cb_chat = str(((cb.get("message") or {}).get("chat") or {}).get("id") or "")
                cb_from = str(((cb.get("from") or {}).get("id") or ""))
                if cb_chat == owner_id:
                    reply, markup = handle_callback(cb.get("data"), owner_id)
                    if reply:
                        tg_send(owner_id, reply, reply_markup=markup)
                    try:
                        tg_api(cfg.get("tg_bot_token"), "answerCallbackQuery",
                               {"callback_query_id": cb.get("id", "")})
                    except Exception:  # noqa: BLE001
                        pass
                continue
            if chat_id == owner_id:
                if not text:
                    continue
                if text.split()[0].split("@")[0].lower() == "/menu":
                    reply, markup = handle_message(text)
                    tg_send(owner_id, reply, reply_markup=markup if isinstance(markup, dict) else _menu_keyboard())
                else:
                    reply, markup = handle_message(text)
                    tg_send(owner_id, reply, reply_markup=markup if isinstance(markup, dict) else None)
                continue
            # --- member / admin (reseller) / calon member ---
            sender = _member_tg(chat_id)
            if sender and webdenz.is_admin(sender):
                reply = handle_admin_message(chat_id, text)
                tg_send(chat_id, reply)
                continue
            if sender:
                photo = ""
                if has_photo and msg.get("photo"):
                    photo = msg["photo"][-1]["file_id"]
                reply, markup = handle_member_message(chat_id, text, has_photo, caption, photo)
                tg_send(chat_id, reply, reply_markup=markup if isinstance(markup, dict) else None)
                if has_photo and photo:
                    # forward foto bukti ke owner
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
