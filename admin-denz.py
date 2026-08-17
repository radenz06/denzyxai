#!/usr/bin/env python3
"""admin-denz — owner panel denzyx web (terminal).

Menu:
  1) Status server
  2) Daftar member
  3) Detail member (lihat password)
  4) Activate member
  5) Ban / Unban member
  6) Extend masa aktif
  7) Lihat log registrasi
  8) Setup config (token TG, harga, owner pass)
  9) Test notifikasi TG
  A) Add member langsung (aktif)
  D) Naikkan/turunkan admin (reseller)
  E) Hapus member (permanen)
  R) Restart server / bot

CLI:
  python3 admin-denz.py add <user> <pass> [hari]
  python3 admin-denz.py delete <user>
  python3 admin-denz.py addadmin <user> | rmadmin <user>
  python3 admin-denz.py bans | unban <ip> | block <ip>
  python3 admin-denz.py visitors [ip|cari] | visitors-clear
"""

import getpass
import json
import os
import secrets
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import lic  # noqa: E402
import auth  # noqa: E402
import webdenz  # noqa: E402
import denzbot  # noqa: E402
import waf  # noqa: E402

PID_DIR = Path(__file__).resolve().parent / "webdata"


def _pids():
    server_pid = bot_pid = None
    if (PID_DIR / "server.pid").exists():
        try:
            server_pid = int((PID_DIR / "server.pid").read_text().strip())
        except ValueError:
            server_pid = None
    if (PID_DIR / "bot.pid").exists():
        try:
            bot_pid = int((PID_DIR / "bot.pid").read_text().strip())
        except ValueError:
            bot_pid = None
    return server_pid, bot_pid


def _alive(pid):
    if not pid:
        return False
    try:
        os.kill(pid, 0)
        return True
    except OSError:
        return False


def _status():
    cfg = webdenz.load_config()
    members = webdenz.list_members()
    active = sum(1 for m in members if webdenz.member_status(m) == "active")
    pending = sum(1 for m in members if webdenz.member_status(m) == "pending")
    banned = sum(1 for m in members if webdenz.member_status(m) == "banned")
    sp, bp = _pids()
    print("=" * 48)
    print("  OWNER PANEL — denzyx web")
    print("=" * 48)
    print(f"  Server     : {cfg.get('host')}:{cfg.get('port')} "
          f"({'JALAN' if _alive(sp) else 'MATI'})")
    print(f"  Bot TG     : {'JALAN' if _alive(bp) else 'MATI'}")
    tp = tunnel_pid()
    print(f"  Tunnel CF  : {'JALAN' if (tp and _alive(tp)) else 'MATI'}"
          + (f"  → {_tunnel_url()}" if (tp and _alive(tp)) else ""))
    print(f"  Member     : {len(members)} total "
          f"(aktif {active} / pending {pending} / banned {banned})")
    print(f"  WAF        : {len(waf.list_bans())} IP diblokir "
          f"(cek via [B], unban via [C] / bot /unbanip)")
    try:
        import track
        track.flush()
        vs = track.summary()
        print(f"  Pengunjung : {vs['total']} IP · {vs['visits']} kunjungan "
              f"(hari ini {vs['today']}, bot {vs['bots']}) — cek via [V]")
    except Exception:  # noqa: BLE001
        pass
    print(f"  Harga      : Rp {cfg.get('price_idr'):,} / "
          f"{cfg.get('sub_days')} hari")
    print(f"  Owner      : {cfg.get('owner', {}).get('username')}")
    print(f"  TG chat id : {cfg.get('tg_chat_id') or '(belum diset)'}")
    print(f"  Config     : {webdenz.CONFIG_PATH}")
    print(f"  Data       : {webdenz.DATA_DIR}")
    print("=" * 48)


def _list_members():
    members = webdenz.list_members()
    if not members:
        print("  Belum ada member.")
        return
    print(f"  {'username':<16} {'role':<7} {'status':<9} {'display':<14} aktif s/d")
    print("  " + "-" * 60)
    for m in sorted(members, key=lambda x: x.get("username", "")):
        role = "ADMIN" if webdenz.is_admin(m) else "member"
        print(f"  {m.get('username',''):<16} {role:<7} "
              f"{webdenz.member_status(m):<9} "
              f"{str(m.get('display_name',''))[:14]:<14} "
              f"{(m.get('expires_at') or '-')[:10]}")


def _detail(username):
    m = webdenz.load_member(username)
    if not m:
        print(f"  Member tidak ada: {username}")
        return
    try:
        pw = webdenz.dec_secret(m["password"])
    except Exception:  # noqa: BLE001
        pw = "(gagal decrypt)"
    print(json.dumps(
        {k: v for k, v in m.items() if k != "messages"},
        ensure_ascii=False, indent=2, default=str))
    print(f"  Role             : {m.get('role') or 'member'}")
    print(f"  Password (decrypt): {pw}")
    print(f"  Sesi md file      : {webdenz.session_md_path(username)}")


def _set_status(username, status, days=None):
    m = webdenz.load_member(username)
    if not m:
        print(f"  Member tidak ada: {username}")
        return
    cfg = webdenz.load_config()
    if status == "active" or status == "unban":
        m["status"] = "active"
        if status == "active":
            m["paid_at"] = webdenz._now_iso()
            if not m.get("expires_at"):
                m["expires_at"] = (webdenz.datetime.now() +
                                   webdenz.timedelta(days=int(cfg.get("sub_days", 30)))).isoformat(timespec="seconds")
    elif status == "banned":
        m["status"] = "banned"
    elif status == "extend":
        base = webdenz._parse_dt(m.get("expires_at"))
        if base < webdenz.datetime.now():
            base = webdenz.datetime.now()
        m["expires_at"] = (base + webdenz.timedelta(days=int(days or 30))).isoformat(timespec="seconds")
        m["status"] = "active"
    webdenz.save_member(m)
    webdenz.write_session_md(m)
    webdenz.log_activity(status, username)
    print(f"  ✅ {username} → {webdenz.member_status(m)} "
          f"(s/d {m.get('expires_at') or '-'})")
    denzbot.tg_notify(f"🛠 {username} → {webdenz.member_status(m)} (admin-denz)")


def _add_member(username, password, days=None):
    """Tambah member langsung aktif (owner & admin = reseller)."""
    username = (username or "").strip()
    password = password or ""
    if not username or not password:
        print("  Pakai: add <username> <password> [hari]")
        return
    if len(username) < 3 or len(password) < 4:
        print("  ✖ username min 3, password min 4 karakter.")
        return
    if webdenz.load_member(username):
        print(f"  ✖ Member sudah ada: {username}")
        return
    m = webdenz.add_member_active(username, password, days=days,
                                  role="member", by="admin-denz")
    print(f"  ✅ {username} AKTIF s/d {m.get('expires_at')}")
    denzbot.tg_notify(f"➕ Member baru (admin-denz): {username}")


def _set_admin(username, admin=True):
    username = (username or "").strip()
    m = webdenz.load_member(username)
    if not m:
        print(f"  ✖ Member tidak ada: {username}")
        return
    m["role"] = "admin" if admin else "member"
    webdenz.save_member(m)
    webdenz.log_activity("addadmin" if admin else "rmadmin", username)
    print(f"  ✅ {username} → {'ADMIN (reseller)' if admin else 'member biasa'}")


def _delete_member(username):
    """Hapus member permanen (file member + sesi + riwayat chat)."""
    username = (username or "").strip()
    m = webdenz.load_member(username)
    if not m:
        print(f"  ✖ Member tidak ada: {username}")
        return
    print(f"  ⚠️  Member: {username} ({m.get('display_name') or '-'})")
    print(f"      Status: {webdenz.member_status(m)}  "
          f"terdaftar {m.get('created_at') or '-'}")
    if input(f"  Yakin hapus {username} PERMANEN? (ketik y): ").strip().lower() != "y":
        print("  Batal.")
        return
    if not webdenz.delete_member(username):
        print(f"  ✖ Gagal menghapus file member: {username}")
        return
    try:
        webdenz.session_md_path(username).unlink()
    except OSError:
        pass
    webdenz.log_activity("delete", f"{username} (admin-denz)")
    denzbot.tg_notify(f"🗑️ Owner hapus member: {username} (admin-denz)")
    print(f"  ✅ {username} dihapus permanen (akun, sesi, riwayat chat).")


def _logs(n=12):
    rows = webdenz.read_log("register", n)
    print("\n".join(f"  {r}" for r in rows) if rows else "  (kosong)")


def _list_bans():
    """Daftar IP yang diblokir WAF."""
    bans = waf.list_bans()
    if not bans:
        print("  ✅ Tidak ada IP yang diblokir.")
        return
    for ip, e in sorted(bans.items(),
                        key=lambda kv: kv[1].get("first_seen_ts") or 0,
                        reverse=True):
        print(f"  ⛔ {ip:<20} {e.get('reason')} "
              f"({int(e.get('count') or 1)}x) [{e.get('last_seen') or '-'}]")
        if e.get("geo"):
            print(f"     📍 {e.get('geo')}")
        if e.get("ua"):
            print(f"     UA: {e.get('ua')[:80]}")


def _unban_ip(ip):
    ip = (ip or "").strip()
    if not waf.unban(ip):
        print(f"  ✖ IP tidak ada di ban list: {ip}")
        return
    webdenz.log_activity("waf_unban", ip)
    denzbot.tg_notify(f"🛡️ Owner unban IP: {ip} (admin-denz)")
    print(f"  ✅ IP {ip} dibuka blokirnya.")


def _block_ip(ip):
    ip = (ip or "").strip()
    if not ip:
        print("  Pakai: block <ip>")
        return
    waf.ban(ip, "manual oleh owner (admin-denz)", path="")
    webdenz.log_activity("waf_manual_ban", ip)
    denzbot.tg_notify(f"⛔ Owner blokir IP manual: {ip} (admin-denz)")
    print(f"  ⛔ IP {ip} diblokir permanen.")


def _list_visitors(n=30, q=""):
    """Daftar pengunjung web (IP, lokasi, software)."""
    import track
    track.flush()
    data = track.load()
    if not data:
        print("  Belum ada pengunjung tercatat.")
        return
    items = list(data.values())
    if q:
        q = q.lower()
        items = [v for v in items if q in str(v.get("ip", "")).lower()
                 or q in (v.get("browser") or "").lower()
                 or q in (v.get("geo") or "").lower()]
    items.sort(key=lambda v: v.get("last_seen") or "", reverse=True)
    bans = waf.list_bans()
    print(f"  {'IP':<18} {'class':<8} {'visits':<6} {'browser':<14} "
          f"{'os':<10} {'device':<8} terakhir")
    print("  " + "-" * 90)
    for v in items[:n]:
        tag = "BANNED" if v.get("ip") in bans else ""
        print(f"  {str(v.get('ip','')):<18} {str(v.get('ip_class','')):<8} "
              f"{int(v.get('visits') or 0):<6} "
              f"{str(v.get('browser') or '-'):<14} "
              f"{str(v.get('os') or '-'):<10} "
              f"{str(v.get('device') or '-'):<8} {v.get('last_seen') or '-'} {tag}")
        if v.get("geo") or v.get("isp"):
            print(f"     📍 {v.get('geo') or '-'} · ISP: {v.get('isp') or '-'}")
        if v.get("cf_ip") and v.get("cf_ip") != v.get("ip"):
            print(f"     CF: {v.get('cf_ip')} · peer: {v.get('peer')}")


def _visitor_detail(ip):
    import track
    track.flush()
    v = track.get(ip)
    if not v:
        print(f"  Visitor tidak ada: {ip}")
        return
    print(f"  IP            : {v.get('ip')} ({v.get('ip_class')})")
    print(f"  Peer (socket) : {v.get('peer')}")
    print(f"  CF-Connecting : {v.get('cf_ip')}")
    print(f"  X-Forwarded-For: {v.get('xff')}")
    print(f"  Lokasi        : {v.get('geo') or '-'}")
    print(f"  ISP / Org     : {v.get('isp') or '-'} / {v.get('org') or '-'}")
    print(f"  Browser/OS    : {v.get('browser')} · {v.get('os')} · {v.get('device')}")
    print(f"  Bot           : {'Ya' if v.get('is_bot') else 'Tidak'}")
    print(f"  Pertama       : {v.get('first_seen')}")
    print(f"  Terakhir      : {v.get('last_seen')}")
    print(f"  Kunjungan     : {v.get('visits')}")
    print(f"  Metode        : " + " ".join(f"{m}={c}" for m, c in (v.get('methods') or {}).items()))
    print(f"  Status        : " + " ".join(f"{s}={c}" for s, c in (v.get('statuses') or {}).items()))
    print(f"  Path          :")
    for p in v.get("paths") or []:
        print(f"     - {p}")
    print(f"  Referer       :")
    for r in v.get("referers") or []:
        print(f"     - {r}")
    if waf.is_banned(ip):
        print(f"  Status WAF    : ⛔ BANNED")
    print(f"  User-Agent    : {v.get('ua')}")


def _clear_visitors():
    import track
    track.clear()
    webdenz.log_activity("visitors_clear", "admin-denz")
    print("  🗑️ Data pengunjung dihapus.")


def _setup():
    cfg = webdenz.load_config()
    cfg.setdefault("owner", {})
    print("  — Setup config —")
    def ask(label, cur):
        v = input(f"  {label} [{cur}]: ").strip()
        return v or cur

    tok = ask("Token bot Telegram", cfg.get("tg_bot_token", ""))
    chat = ask("Chat id owner", cfg.get("tg_chat_id", ""))
    cfg["tg_bot_token"] = tok
    cfg["tg_chat_id"] = chat
    owner_user = ask("Username owner", cfg["owner"].get("username", "denzyx"))
    cfg["owner"]["username"] = owner_user
    pw = getpass.getpass("  Password owner baru (kosong = biarkan): ")
    if pw:
        salt = secrets.token_hex(8)
        cfg["owner"]["salt"] = salt
        cfg["owner"]["password_hash"] = webdenz.hash_password(pw, salt)
    if not cfg.get("secret"):
        cfg["secret"] = secrets.token_hex(32)
    price = ask("Harga (Rp)", str(cfg.get("price_idr", 20000)))
    days = ask("Durasi (hari)", str(cfg.get("sub_days", 30)))
    cfg["price_idr"] = int(price)
    cfg["sub_days"] = int(days)
    qr_url = ask("Link QR pembayaran (catbox/hosting, kosong=file lokal)",
                 cfg.get("qr_url", ""))
    cfg["qr_url"] = qr_url
    host = ask("Bind server (keamanan: 127.0.0.1 = akses via tunnel saja)",
               cfg.get("host", "127.0.0.1"))
    cfg["host"] = host if host else "127.0.0.1"
    webdenz.save_config(cfg)
    print(f"  ✅ Config tersimpan: {webdenz.CONFIG_PATH}")
    if tok and chat:
        r = denzbot.tg_notify("✅ Owner panel denzyx terhubung ke bot.")
        print("  ✅ Notifikasi TG:", "OK" if r.get("ok") else r.get("description"))


def _test_tg():
    r = denzbot.tg_notify("🔔 Tes notifikasi dari admin-denz.")
    if r.get("ok"):
        print("  ✅ Notifikasi terkirim.")
    else:
        print(f"  ❌ Gagal: {r.get('description')}")


def _restart(which):
    sp, bp = _pids()
    script = None
    if which == "server":
        script = Path(__file__).resolve().parent / "webdenz.py"
        old = sp
        pidfile = PID_DIR / "server.pid"
    else:
        script = Path(__file__).resolve().parent / "denzbot.py"
        old = bp
        pidfile = PID_DIR / "bot.pid"
    if old and _alive(old):
        os.kill(old, 9)
        time.sleep(0.5)
    import subprocess
    log = PID_DIR / f"{which}.log"
    logf = open(log, "a", encoding="utf-8")
    proc = subprocess.Popen(
        [sys.executable, str(script)],
        cwd=str(script.parent),
        stdout=logf, stderr=subprocess.STDOUT)
    pidfile.write_text(str(proc.pid))
    print(f"  🔄 {which} start → pid {proc.pid} (log: {log})")


def _stop(which):
    sp, bp = _pids()
    pid = sp if which == "server" else bp
    if not pid:
        print(f"  {which} tidak jalan")
        return
    try:
        os.kill(pid, 9)
        print(f"  ⏹ {which} (pid {pid}) dihentikan")
    except OSError as e:
        print(f"  ⏹ {which}: {e}")
    (PID_DIR / f"{which}.pid").unlink(missing_ok=True)


def tunnel_pid():
    try:
        if (PID_DIR / "tunnel.pid").exists():
            return int((PID_DIR / "tunnel.pid").read_text().strip())
    except ValueError:
        return None
    return None


def _tunnel_url():
    log = (PID_DIR / "tunnel.log")
    if not log.exists():
        return ""
    for line in reversed(log.read_text(encoding="utf-8", errors="replace")
                        .splitlines()):
        for tok in line.split():
            if "trycloudflare.com" in tok:
                return tok.strip()
    return ""


def _start_tunnel():
    pid = tunnel_pid()
    if pid and _alive(pid):
        print(f"  ✓ tunnel jalan (pid {pid}) → {_tunnel_url()}")
        return
    import shutil
    which = shutil.which("cloudflared")
    if not which:
        print("  ❌ cloudflared tidak terpasang (pkg install cloudflared)")
        return
    import subprocess
    log = PID_DIR / "tunnel.log"
    logf = open(log, "a", encoding="utf-8")
    port = int(webdenz.load_config().get("port") or 8000)
    proc = subprocess.Popen(
        [which, "tunnel", "--url", f"http://localhost:{port}",
         "--protocol", "http2", "--no-autoupdate"],
        stdout=logf, stderr=subprocess.STDOUT, start_new_session=True)
    PID_DIR.joinpath("tunnel.pid").write_text(str(proc.pid))
    print(f"  🔄 tunnel start → pid {proc.pid} (URL menyusul ~5-10 dtk)")


def _stop_tunnel():
    pid = tunnel_pid()
    if not pid:
        print("  tunnel tidak jalan")
        return
    try:
        os.kill(pid, 9)
        print(f"  ⏹ tunnel (pid {pid}) dihentikan")
    except OSError as e:
        print(f"  ⏹ tunnel: {e}")
    (PID_DIR / "tunnel.pid").unlink(missing_ok=True)


def _ensure():
    """Pastikan server + bot + tunnel jalan (start kalau mati). Return bool semua ok."""
    sp, bp = _pids()
    result = True
    if not (sp and _alive(sp)):
        _restart("server")
        result = False
    else:
        print(f"  ✓ server jalan (pid {sp})")
    if not (bp and _alive(bp)):
        _restart("bot")
        result = False
    else:
        print(f"  ✓ bot jalan (pid {bp})")
    tp = tunnel_pid()
    if not (tp and _alive(tp)):
        _start_tunnel()
        result = False
    else:
        print(f"  ✓ tunnel jalan (pid {tp}) → {_tunnel_url()}")
    return result


def _restart_all():
    _stop("server")
    _stop("bot")
    _stop_tunnel()
    _restart("server")
    _restart("bot")
    _start_tunnel()


def _menu():
    _status()
    print("""
  [1] Daftar member          [6] Extend masa aktif
  [2] Detail member          [7] Log registrasi
  [3] Activate member        [8] Setup config
  [4] Ban member             [9] Test notifikasi TG
  [5] Unban member           [A] Add member langsung
                            [B] IP diblokir WAF
                            [C] Unban IP WAF
                            [D] Naikkan/turunkan admin
  [E] Hapus member (permanen)
                            [V] Pengunjung web   [W] Hapus data pengunjung
                            [R] Restart server/bot
                            [P] Ganti password lisensi
                            [T] Start tunnel     [U] URL tunnel
                            [Q] Keluar
  """)
    c = input("  > ").strip().lower()
    if c == "1":
        _list_members()
    elif c == "2":
        _detail(input("  username: ").strip())
    elif c == "3":
        _set_status(input("  username: ").strip(), "active")
    elif c == "4":
        _set_status(input("  username: ").strip(), "banned")
    elif c == "5":
        _set_status(input("  username: ").strip(), "unban")
    elif c == "6":
        u = input("  username: ").strip()
        d = input("  hari tambahan: ").strip()
        _set_status(u, "extend", d)
    elif c == "7":
        _logs()
    elif c == "8":
        _setup()
    elif c == "9":
        _test_tg()
    elif c == "b":
        _list_bans()
    elif c == "c":
        _unban_ip(input("  IP yang mau di-unban: ").strip())
    elif c == "a":
        _add_member(input("  username: ").strip(),
                    input("  password: ").strip(),
                    input("  hari (kosong=default): ").strip() or None)
    elif c == "d":
        _set_admin(input("  username: ").strip(),
                   admin=input("  jadikan admin? (y/n): ").strip().lower()
                   == "y")
    elif c == "e":
        _delete_member(input("  username: ").strip())
    elif c == "v":
        _list_visitors(30)
    elif c == "w":
        _clear_visitors()
    elif c == "p":
        lic.setpass()
    elif c == "t":
        _start_tunnel()
    elif c == "u":
        print(" ", _tunnel_url())
    elif c == "r":
        _restart(input("  server atau bot? ").strip().lower())
    elif c == "q":
        return False
    else:
        print("  (perintah tidak dikenal)")
    return True


def main():
    webdenz._mkdirs()
    PID_DIR.mkdir(parents=True, exist_ok=True)
    # gerbang lisensi + login OWNER untuk seluruh admin CLI (owner panel).
    try:
        lic.require()
    except SystemExit:
        raise
    except Exception:  # noqa: BLE001
        pass
    try:
        auth.require_terminal(owner_only=True)
    except SystemExit:
        raise
    except Exception:  # noqa: BLE001
        pass
    if len(sys.argv) > 1:
        # mode CLI langsung
        arg = sys.argv[1].lower()
        if arg == "setpass":
            sys.exit(lic.setpass())
        if arg == "setup":
            _setup()
        elif arg == "list":
            _list_members()
        elif arg == "add":
            u = sys.argv[2] if len(sys.argv) > 2 else ""
            p = sys.argv[3] if len(sys.argv) > 3 else ""
            d = sys.argv[4] if len(sys.argv) > 4 and sys.argv[4].isdigit() else None
            _add_member(u, p, d)
        elif arg == "delete":
            _delete_member(sys.argv[2] if len(sys.argv) > 2 else "")
        elif arg == "addadmin":
            _set_admin(sys.argv[2] if len(sys.argv) > 2 else "", True)
        elif arg == "rmadmin":
            _set_admin(sys.argv[2] if len(sys.argv) > 2 else "", False)
        elif arg == "bans":
            _list_bans()
        elif arg == "visitors":
            extra = sys.argv[2] if len(sys.argv) > 2 else ""
            if extra and (extra.count(".") >= 1 or ":" in extra):
                _visitor_detail(extra)
            else:
                _list_visitors(30, q=extra)
        elif arg == "visitors-clear":
            _clear_visitors()
        elif arg == "unban":
            _unban_ip(sys.argv[2] if len(sys.argv) > 2 else "")
        elif arg == "block":
            _block_ip(sys.argv[2] if len(sys.argv) > 2 else "")
        elif arg == "status":
            _status()
        elif arg == "start-bot":
            _restart("bot")
        elif arg == "stop-bot":
            _stop("bot")
        elif arg == "start-server":
            _restart("server")
        elif arg == "stop-server":
            _stop("server")
        elif arg == "ensure":
            _ensure()
        elif arg == "restart":
            _restart_all()
        elif arg == "start-tunnel":
            _start_tunnel()
        elif arg == "stop-tunnel":
            _stop_tunnel()
        elif arg == "url":
            print(_tunnel_url())
        else:
            print(__doc__)
        return
    while _menu():
        input("  (Enter lanjut) ")


if __name__ == "__main__":
    main()
