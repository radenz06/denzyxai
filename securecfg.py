#!/usr/bin/env python3
"""securecfg — simpan/baca konfigurasi terenkripsi (Fernet, key lokal).

webconfig.json di-disk dalam bentuk terenkripsi supaya rahasia
(tg bot token, password hash, secret, dll) tidak terbaca mentah oleh
siapapun yang memegang file config-nya.

- Key Fernet (32 byte random) disimpan terpisah di webdata/.config.key
  (gitignored, chmod 600). Jangan commit key-nya.
- Bisa di-override via env WEBDENZ_CFG_KEY (mis. untuk portability).
- Kompatibel dengan config plaintext versi lama: saat read(), file
  plaintext otomatis di-baca lalu di-migrasi ke terenkripsi.

Dipakai oleh webdenz.py dan lic.py. Tidak butuh dependency ekstra
(cryptography sudah dipakai webdenz).
"""

import base64
import hashlib
import json
import os
import secrets
import sys
from pathlib import Path


def default_config_path():
    return Path(os.environ.get("WEBDENZ_CONFIG")
                or Path(__file__).resolve().parent / "webconfig.json")


def default_data_dir():
    return Path(os.environ.get("WEBDENZ_DATA")
                or Path(__file__).resolve().parent / "webdata")


def cfg_key(data_dir=None):
    """Key Fernet lokal (dibuat sekali, chmod 600). Override via env."""
    env = os.environ.get("WEBDENZ_CFG_KEY")
    if env:
        return env.encode()
    kf = (Path(data_dir) if data_dir else default_data_dir()) / ".config.key"
    try:
        if kf.exists():
            return kf.read_bytes()
        kf.parent.mkdir(parents=True, exist_ok=True)
        key = base64.urlsafe_b64encode(
            hashlib.sha256(secrets.token_bytes(32)).digest())
        kf.write_bytes(key)
        try:
            os.chmod(kf, 0o600)
        except OSError:
            pass
        return key
    except OSError:
        # fallback (dir tak bisa ditulis): key turunan dari mesin + path.
        return base64.urlsafe_b64encode(hashlib.sha256(
            (os.uname().nodename + str(default_config_path())).encode()).digest())


def encrypt(data, data_dir=None):
    from cryptography.fernet import Fernet
    raw = json.dumps(data, ensure_ascii=False, indent=2).encode("utf-8")
    return Fernet(cfg_key(data_dir)).encrypt(raw).decode("ascii")


def decrypt(text, data_dir=None):
    from cryptography.fernet import Fernet
    raw = Fernet(cfg_key(data_dir)).decrypt(text.encode()).decode("utf-8")
    return json.loads(raw)


def read(path=None, data_dir=None):
    """Baca config → dict; None bila file tak ada / rusak.

    File plaintext lama (versi sebelum enkripsi) otomatis di-migrasi
    ke terenkripsi di sini.
    """
    p = Path(path) if path else default_config_path()
    try:
        content = p.read_text(encoding="utf-8")
    except OSError:
        return None
    content = content.strip()
    if not content:
        return None
    if content.startswith("{"):
        try:
            data = json.loads(content)
        except json.JSONDecodeError:
            return None
        try:
            write(data, path=p, data_dir=data_dir)  # migrasi → terenkripsi
        except OSError:
            pass
        return data
    try:
        return decrypt(content, data_dir)
    except Exception:  # noqa: BLE001 — InvalidToken dll.
        print("[securecfg] ⚠️ webconfig.json tidak bisa di-decrypt "
              "(key berubah / file rusak). Pakai config default.",
              file=sys.stderr)
        return None


def write(data, path=None, data_dir=None):
    p = Path(path) if path else default_config_path()
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(encrypt(data, data_dir), encoding="utf-8")
