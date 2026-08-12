#!/usr/bin/env python3
"""lic — gerbang lisensi denzyx AI.

Setiap kali project dijalankan, terminal meminta password lisensi.
Tanpa password yang benar, program TIDAK bisa dijalankan (exit).

Keamanan:
- Password TIDAK pernah disimpan mentah (plaintext) di mana pun.
- Yang disimpan hanya PBKDF2 hash (120k iterasi) + salt, dan keduanya
  di-obfuscate (XOR + base64) supaya tidak terbaca langsung dari kode.
- Owner mengganti password lewat: python3 admin-denz.py setpass
  (harus tahu password lama). Hash baru tersimpan di webconfig.json.
"""

import base64
import getpass
import hashlib
import hmac
import os
import secrets
import sys
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import securecfg  # noqa: E402  (webconfig.json terenkripsi at-rest)

HERE = Path(__file__).resolve().parent
CONFIG_PATH = Path(os.environ.get("WEBDENZ_CONFIG")
                   or HERE / "webconfig.json")

_KEY = 0x5A
_ITER = 120_000
TOKEN_PATH = Path(os.environ.get("WEBDENZ_TOKEN")
                  or HERE / "webdata" / ".lic_ok")

# default hash + salt (fallback bila webconfig.json belum punya lisensi).
# Ter-obfuscate: bukan password, tapi hash — tidak bisa dibalik jadi password.
_DEFAULT_SALT = 'Oz48O29sbG1iaDg+OTk4PGxvbzw5YmNsYmw5a2hvODs='
_DEFAULT_DIGEST = 'a208Yjhsajk+a2NvYm9jYzhuP288OTlpOD9tPjttY25jYzg5YmI/ams+PDk8bT4+aDhrbGpobD5tPGw/PD5sOw=='


def _xor(s):
    return bytes(ord(c) ^ _KEY for c in s)


def _deobf(s):
    return "".join(chr(b ^ _KEY) for b in base64.b64decode(s))


def _obf(s):
    return base64.b64encode(_xor(s)).decode()


def _lic():
    """Ambil (salt, digest) dari webconfig.json, fallback ke default."""
    try:
        data = securecfg.read(CONFIG_PATH) or {}
        lic = data.get("lic") or {}
        if lic.get("salt") and lic.get("digest"):
            return _deobf(lic["salt"]), _deobf(lic["digest"])
    except Exception:  # noqa: BLE001
        pass
    return _deobf(_DEFAULT_SALT), _deobf(_DEFAULT_DIGEST)


def hash_pw(pw, salt):
    return hashlib.pbkdf2_hmac("sha256", pw.encode(), salt.encode(),
                               _ITER).hex()


def verify(pw):
    salt, digest = _lic()
    if not digest or not salt:
        return False
    return hmac.compare_digest(hash_pw(pw, salt), digest)


def unlocked():
    """Sudah terverifikasi sesi ini (env) ATAU mesin ini punya token file."""
    if os.environ.get("DENZYX_LIC") == "ok":
        return True
    try:
        if not TOKEN_PATH.exists():
            return False
        return TOKEN_PATH.read_text().strip() == _token_value()
    except OSError:
        return False


def _token_value():
    _, digest = _lic()
    return hashlib.sha256(("denzyx:" + digest).encode()).hexdigest()


def _write_token():
    """Buat token lisensi lokal (mesin ini) supaya daemon/restart tanpa prompt."""
    try:
        TOKEN_PATH.parent.mkdir(parents=True, exist_ok=True)
        TOKEN_PATH.write_text(_token_value(), encoding="utf-8")
    except OSError:
        pass


def require():
    """Wajib password. Kalau salah → print + exit. Return True kalau lolos."""
    if unlocked():
        return True
    pw = os.environ.get("DENZYX_PASS")  # bisa disuplai non-interaktif
    if pw and verify(pw):
        os.environ["DENZYX_LIC"] = "ok"
        _write_token()
        return True
    if not sys.stdin.isatty():
        print("🔒 Project terkunci: butuh password lisensi. "
              "Jalankan dulu 'python3 denzyx.py' untuk memasukkan password "
              "sekali (token disimpan di mesin ini). "
              "Atau set env DENZYX_PASS.",
              file=sys.stderr)
        sys.exit(2)
    try:
        pw = getpass.getpass("🔒 Password lisensi denzyx AI: ")
    except (EOFError, KeyboardInterrupt):
        pw = ""
    if pw and verify(pw):
        os.environ["DENZYX_LIC"] = "ok"
        _write_token()
        return True
    print("✖ Password lisensi salah. Project tidak bisa dijalankan.",
          file=sys.stderr)
    sys.exit(1)


def setpass():
    """Ganti password lisensi. Butuh password lama + konfirmasi baru."""
    if not sys.stdin.isatty():
        print("setpass butuh terminal interaktif.", file=sys.stderr)
        return 1
    try:
        old = getpass.getpass("Password lisensi lama: ")
    except (EOFError, KeyboardInterrupt):
        return 1
    if not verify(old):
        print("✖ Password lama salah.", file=sys.stderr)
        return 1
    try:
        p1 = getpass.getpass("Password lisensi baru: ")
        p2 = getpass.getpass("Ulangi password baru: ")
    except (EOFError, KeyboardInterrupt):
        return 1
    if len(p1) < 6:
        print("✖ Password baru terlalu pendek (min 6 karakter).",
              file=sys.stderr)
        return 1
    if p1 != p2:
        print("✖ Konfirmasi tidak cocok.", file=sys.stderr)
        return 1
    salt = secrets.token_hex(16)
    digest = hash_pw(p1, salt)
    data = securecfg.read(CONFIG_PATH)
    if data is None:
        # config tak terbaca (rusak / key berubah): jangan tulis cuma {lic}
        # — itu akan menghapus sisa konfigurasi (token TG, secret, dll).
        print("✖ webconfig.json tidak terbaca (rusak / key berubah). "
              "Periksa dulu sebelum ganti lisensi.", file=sys.stderr)
        return 1
    data["lic"] = {"salt": _obf(salt), "digest": _obf(digest)}
    securecfg.write(data, CONFIG_PATH)
    print("✅ Password lisensi diganti.")
    return 0
