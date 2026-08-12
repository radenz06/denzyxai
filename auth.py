#!/usr/bin/env python3
"""auth — gate login member/owner untuk entry point terminal (TUI, voice,
daemon, admin).

Setelah password LISENSI benar, user belum bisa akses: mereka harus login
dulu sebagai member aktif (berbayar) atau owner. Kalau bukan member sama
sekali → arahkan untuk registrasi via Telegram owner.

Sesi:
- Setelah login berhasil, token sesi lokal disimpan (webdata/.term_session,
  gitignored) supaya subproses & daemon (jalan headless) lolos tanpa
  prompt berulang. Berlaku 7 hari.
- Induk yang sudah login mewariskan env DENZYX_TERM_USER / DENZYX_TERM_ROLE
  ke subproses → mereka lolos otomatis.

Role:
- owner  → akses penuh (termasuk admin CLI / owner panel).
- admin  → reseller: akses TUI + menambah member (via bot/web), bukan
  owner panel.
- member → hanya kalau status "active" (langganan berbayar). pending /
  expired / banned ditolak sesuai status.
"""

import getpass
import json
import os
import secrets
import sys
from datetime import datetime, timedelta
from pathlib import Path

HERE = Path(__file__).resolve().parent
SESSION_PATH = Path(os.environ.get("WEBDENZ_SESSION")
                    or HERE / "webdata" / ".term_session")
SESSION_DAYS = int(os.environ.get("DENZYX_SESSION_DAYS") or 7)


def _now_iso():
    return datetime.now().isoformat(timespec="seconds")


def _owner_contact():
    """Kontak owner dari config — fallback teks umum."""
    try:
        import webdenz
        uname = (webdenz.load_config().get("tg_owner_username") or "").strip()
        if uname:
            return f"t.me/{uname}"
    except Exception:  # noqa: BLE001
        pass
    return "owner"


def login(username, password):
    """Verifikasi kredensial. Return dict role/status, atau None kalau
    username/password tidak dikenali."""
    import webdenz
    if not username or not password:
        return None
    cfg = webdenz.load_config()
    own = cfg.get("owner") or {}
    if username == own.get("username"):
        if not own.get("password_hash"):
            return {"role": "owner", "username": username,
                    "status": "owner_bootstrap"}
        if webdenz.verify_password(
                password, own.get("salt", ""), own.get("password_hash", "")):
            return {"role": "owner", "username": username, "status": "active"}
    m = webdenz.load_member(username)
    if not m or not m.get("password"):
        return None
    try:
        stored = webdenz.dec_secret(m["password"])
        if not secrets.compare_digest(stored, password):
            return None
    except Exception:  # noqa: BLE001
        return None
    role = "admin" if webdenz.is_admin(m) else "member"
    return {"role": role, "username": username,
            "status": webdenz.member_status(m)}


def _bootstrap_owner(username):
    """Password owner belum diset (first-run) → buat sekarang. Return
    (ok, pesan). Butuh terminal interaktif."""
    import webdenz
    try:
        p1 = getpass.getpass("  Password owner baru: ")
        p2 = getpass.getpass("  Ulangi password owner: ")
    except (EOFError, KeyboardInterrupt):
        return False, "Dibatalkan."
    if len(p1) < 6:
        return False, "Password owner terlalu pendek (min 6 karakter)."
    if p1 != p2:
        return False, "Konfirmasi password tidak cocok."
    cfg = webdenz.load_config()
    cfg.setdefault("owner", {})
    salt = secrets.token_hex(8)
    cfg["owner"]["username"] = username
    cfg["owner"]["salt"] = salt
    cfg["owner"]["password_hash"] = webdenz.hash_password(p1, salt)
    webdenz.save_config(cfg)
    return True, f"Password owner '{username}' dibuat."


def _env_ok():
    return (os.environ.get("DENZYX_TERM_USER")
            and os.environ.get("DENZYX_TERM_ROLE")
            in ("owner", "member", "admin"))


def _load_session():
    try:
        data = json.loads(SESSION_PATH.read_text(encoding="utf-8"))
        exp = datetime.fromisoformat(data["expires"])
        if exp < datetime.now():
            return None
        if data.get("role") not in ("owner", "member", "admin"):
            return None
        return data
    except (OSError, json.JSONDecodeError, KeyError, ValueError):
        return None


def _write_session(role, username):
    try:
        SESSION_PATH.parent.mkdir(parents=True, exist_ok=True)
        data = {"role": role, "username": username,
                "expires": (datetime.now() + timedelta(days=SESSION_DAYS))
                .isoformat(timespec="seconds")}
        SESSION_PATH.write_text(json.dumps(data, ensure_ascii=False),
                                encoding="utf-8")
    except OSError:
        pass


def _apply_env(role, username):
    os.environ["DENZYX_TERM_USER"] = username
    os.environ["DENZYX_TERM_ROLE"] = role


def _reject(message, code=1):
    print(message, file=sys.stderr)
    sys.exit(code)


def require_terminal(owner_only=False):
    """Wajib login member aktif / owner. Exit kalau tidak berhak."""
    if _env_ok():
        role = os.environ.get("DENZYX_TERM_ROLE")
        if owner_only and role != "owner":
            _reject("✖ Akses admin khusus owner.", 1)
        return True
    sess = _load_session()
    if sess:
        if owner_only and sess.get("role") != "owner":
            _reject("✖ Akses admin khusus owner.", 1)
        _apply_env(sess["role"], sess["username"])
        return True
    # non-interaktif: coba dari env username/password (mis. daemon)
    u = os.environ.get("DENZYX_USER")
    p = os.environ.get("DENZYX_PASS")
    if u and p:
        info = login(u, p)
        if info and info["status"] == "active":
            if owner_only and info["role"] != "owner":
                _reject("✖ Akses admin khusus owner.", 1)
            _write_session(info["role"], info["username"])
            _apply_env(info["role"], info["username"])
            return True
    if not sys.stdin.isatty():
        _reject("🔒 Butuh login member/owner. Jalankan dulu './denzyx' untuk "
                "login sekali (sesi tersimpan di mesin ini). Atau set "
                "env DENZYX_USER + DENZYX_PASS.", 2)
    # interaktif
    import webdenz as _wd
    _own = (_wd.load_config().get("owner") or {})
    if owner_only and not _own.get("password_hash"):
        print("🔒 Password owner belum dibuat (first-run).")
        try:
            username = input(f"  username owner [{_own.get('username') or 'denzyx'}]: ").strip() \
                or _own.get("username") or "denzyx"
        except (EOFError, KeyboardInterrupt):
            _reject("✖ Dibatalkan.", 1)
        ok, msg = _bootstrap_owner(username)
        if not ok:
            _reject("✖ " + msg, 1)
        _write_session("owner", username)
        _apply_env("owner", username)
        print("  " + msg)
        return True
    print("🔒 Masuk sebagai member/owner untuk pakai denzyx AI.")
    try:
        username = input("  username: ").strip()
        password = getpass.getpass("  password: ")
    except (EOFError, KeyboardInterrupt):
        _reject("✖ Dibatalkan.", 1)
    if not username or not password:
        _reject("✖ Username/password tidak boleh kosong.", 1)
    info = login(username, password)
    if info is None:
        _reject("✖ Bukan member. Minta link registrasi ke Telegram: "
                + _owner_contact(), 1)
    if info.get("status") == "owner_bootstrap":
        # owner belum punya password → buat sekarang (first-run)
        ok, msg = _bootstrap_owner(username)
        if not ok:
            _reject("✖ " + msg, 1)
        _write_session("owner", username)
        _apply_env("owner", username)
        print("  " + msg)
        return True
    if info["status"] != "active":
        msgs = {"pending": "✖ Akun menunggu konfirmasi pembayaran — "
                           "hubungi Telegram " + _owner_contact() + ".",
                "expired": "✖ Langganan kedaluwarsa — perpanjang lewat "
                           + _owner_contact() + ".",
                "banned": "✖ Akun diblokir (banned)."}
        _reject(msgs.get(info["status"], "✖ Status tidak aktif."), 1)
    if owner_only and info["role"] != "owner":
        _reject("✖ Akses admin khusus owner.", 1)
    _write_session(info["role"], info["username"])
    _apply_env(info["role"], info["username"])
    return True
