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
  10) Restart server / bot
"""

import getpass
import json
import os
import secrets
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import webdenz  # noqa: E402
import denzbot  # noqa: E402

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
    print(f"  Member     : {len(members)} total "
          f"(aktif {active} / pending {pending} / banned {banned})")
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
    print(f"  {'username':<16} {'status':<9} {'display':<14} aktif s/d")
    print("  " + "-" * 56)
    for m in sorted(members, key=lambda x: x.get("username", "")):
        print(f"  {m.get('username',''):<16} "
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


def _logs(n=12):
    rows = webdenz.read_log("register", n)
    print("\n".join(f"  {r}" for r in rows) if rows else "  (kosong)")


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
    proc = subprocess.Popen(
        [sys.executable, str(script)],
        cwd=str(script.parent),
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    pidfile.write_text(str(proc.pid))
    print(f"  🔄 {which} restart → pid {proc.pid}")


def _menu():
    _status()
    print("""
  [1] Daftar member          [6] Extend masa aktif
  [2] Detail member          [7] Log registrasi
  [3] Activate member        [8] Setup config
  [4] Ban member             [9] Test notifikasi TG
  [5] Unban member           [R] Restart server/bot
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
    if len(sys.argv) > 1:
        # mode CLI langsung
        arg = sys.argv[1].lower()
        if arg == "setup":
            _setup()
        elif arg == "list":
            _list_members()
        elif arg == "status":
            _status()
        else:
            print(__doc__)
        return
    while _menu():
        input("  (Enter lanjut) ")


if __name__ == "__main__":
    main()
