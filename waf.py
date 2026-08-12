#!/usr/bin/env python3
"""waf — Web Application Firewall mini untuk webdenz (di balik cloudflared).

Lapisan keamanan tambahan:
- IP asli klien: baca CF-Connecting-IP / X-Forwarded-For, tapi HANYA
  dipercaya dari koneksi loopback (mencegah spoof header dari akses langsung).
- Deteksi serangan per-request: User-Agent alat peretas (Burp, sqlmap,
  nikto, dll), honeypot path (wp-login.php, phpmyadmin, .git, dll),
  path traversal (../, %2e%2e), pola injection (union select, <script>,
  /etc/passwd, dll).
- Endpoint scan: 404 ke banyak path acak dalam waktu singkat → ban.
- Brute-force: gagal login / kena rate-limit berulang → ban.
- Ban IP PERMANEN, persisten ke webdata/bans.json (thread-safe).
- Notifikasi Telegram ke owner: IP + lokasi geografis + UA + path + waktu.
- API: get_real_ip / is_banned / ban / unban / list_bans / flag / record_404.

Tanpa dependency eksternal (urllib saja). Dipakai oleh webdenz.py,
denzbot.py, dan admin-denz.py.
"""

import ipaddress
import json
import os
import re
import threading
import time
import urllib.request
from datetime import datetime
from pathlib import Path

# ---------------------------------------------------------------------------
# Lokasi data (ikuti env WEBDENZ_DATA supaya isolasi test jalan)
# ---------------------------------------------------------------------------

_BANS_FILE = None


def _bans_file():
    global _BANS_FILE
    if _BANS_FILE is None:
        base = Path(os.environ.get("WEBDENZ_DATA")
                    or Path(__file__).resolve().parent / "webdata")
        _BANS_FILE = base / "bans.json"
    return _BANS_FILE


def _load_cfg():
    import webdenz  # lazy, hindari circular import
    return webdenz.load_config()


def _is_loopback(ip):
    try:
        return ipaddress.ip_address(str(ip)).is_loopback
    except ValueError:
        return str(ip) in ("", "localhost", "::1", "127.0.0.1")


# ---------------------------------------------------------------------------
# Pola serangan
# ---------------------------------------------------------------------------

_ATTACK_UA = re.compile(
    r"burp|nikto|sqlmap|nmap|masscan|zgrab|gobuster|ffuf|feroxbuster|"
    r"dirsearch|dirb|hydra|nuclei|whatweb|wpscan|johntheripper|acunetix|"
    r"nessus|netsparker|appscan|w3af|arachni|metasploit|hydra|openvas|"
    r"paros|fimap|joomscan|sinfp|wfetch|morfeus|uniscan|nessus|netsparker|"
    r"scrutinizer|\.net\.core|censys|shodan|massscan|zmeu|test[\s_-]*user.?agent",
    re.I)

_HONEYPOT = [
    "wp-login.php", "wp-admin", "wp-content", "wp-includes", "xmlrpc.php",
    "phpmyadmin", "/pma", "adminer", "cgi-bin", "shell.php", "cmd.php",
    "default.ida", ".git/config", "server-status", "server-info",
    "actuator", "swagger", "graphql", "_next", "vendor/composer",
    "wp-json", "authoradmin", "dashboard", ".well-known/security.txt",
]

_INJ_PAT = re.compile(
    r"(union[ ]+select|select[ ]+.*from[ ]+|insert[ ]+into[ ]+|"
    r"drop[ ]+table[ ]+|update[ ]+.*set[ ]+|create[ ]+table[ ]+|"
    r"<\s*script|javascript:|onerror=|onload=|eval\s*\(|exec\s*\(|"
    r"system\s*\(|passthru\s*\(|base64[_-]?decode|/etc/passwd|"
    r"proc/self|\.bash_history|\.ssh[^/]|id_rsa|webconfig\.json|"
    r"config\.yml|\.env[^a-z]|\.htaccess|\.git[^a-z]|\.\./|\.\.%2f|"
    r"%2e%2e|%00|cmd=|wget[ ]+http|curl[ ]+http|/cgi-bin/|"
    r"information_schema|xp_cmdshell|pg_sleep|benchmark\s*\(|"
    r"['\"]\s+or\s+['\"]|['\"]\s*--|--['\"]|#['\"])",
    re.I)


def scan_signal(ip, ua, path, query=""):
    """Kembalikan deskripsi sinyal serangan (string) atau None bila aman."""
    if _is_loopback(ip):
        # akses langsung lokal / koneksi tanpa info proxy — jangan auto-ban,
        # tapi tetap cek sinyal untuk bisa menolak request ini sekali saja.
        pass
    full = (path or "") + ("?" + query if query else "")
    low = full.lower()
    if _ATTACK_UA.search(ua or ""):
        return f"user-agent alat peretas: {(ua or '')[:64]}"
    for hp in _HONEYPOT:
        if hp in low:
            return f"honeypot path '{hp}'"
    if ".." in (path or "") or "%2e%2e" in low:
        return "path traversal"
    if _INJ_PAT.search(full):
        return "pola injection"
    return None


# ---------------------------------------------------------------------------
# Store ban (persisten, thread-safe)
# ---------------------------------------------------------------------------

def _now_iso():
    return datetime.now().isoformat(timespec="seconds")


class _BanStore:
    def __init__(self):
        self._lock = threading.Lock()
        self._bans = {}
        self._loaded_dir = None

    def _datadir(self):
        return str(Path(os.environ.get("WEBDENZ_DATA")
                        or Path(__file__).resolve().parent / "webdata"))

    def _ensure_loaded(self):
        """Reload bila dir data berubah (mis. isolasi antar-test)."""
        d = self._datadir()
        if self._loaded_dir != d:
            self._bans = {}
            try:
                data = json.loads(_bans_file().read_text(encoding="utf-8"))
                if isinstance(data, dict):
                    self._bans = data
            except (OSError, json.JSONDecodeError):
                self._bans = {}
            self._loaded_dir = d

    def _save(self):
        try:
            _bans_file().parent.mkdir(parents=True, exist_ok=True)
            _bans_file().write_text(
                json.dumps(self._bans, ensure_ascii=False, indent=2),
                encoding="utf-8")
        except OSError:
            pass

    def get(self, ip):
        with self._lock:
            self._ensure_loaded()
            e = self._bans.get(ip)
            return dict(e) if e else None

    def is_banned(self, ip):
        with self._lock:
            self._ensure_loaded()
            return ip in self._bans

    def add(self, ip, reason, ua="", path=""):
        """Catat kejadian. Return (entry_baru_dict, perlu_notify)."""
        with self._lock:
            self._ensure_loaded()
            now = time.time()
            e = self._bans.get(ip)
            if e is None:
                e = {"reason": reason, "ua": (ua or "")[:160],
                     "path": (path or "")[:250], "count": 1,
                     "first_seen": _now_iso(), "first_seen_ts": now,
                     "last_seen": _now_iso(), "last_seen_ts": now,
                     "geo": "", "notified_at": 0.0}
                self._bans[ip] = e
                is_new = True
            else:
                e["count"] = int(e.get("count") or 1) + 1
                e["last_seen"] = _now_iso()
                e["last_seen_ts"] = now
                e["reason"] = reason
                e["ua"] = (ua or e.get("ua") or "")[:160]
                e["path"] = (path or e.get("path") or "")[:250]
                is_new = False
            notify = is_new or (now - float(e.get("notified_at") or 0)) > 900
            if notify:
                e["notified_at"] = now
            self._save()
            return dict(e), notify

    def remove(self, ip):
        with self._lock:
            self._ensure_loaded()
            ok = self._bans.pop(ip, None) is not None
            self._save()
            return ok

    def all(self):
        with self._lock:
            self._ensure_loaded()
            return {k: dict(v) for k, v in self._bans.items()}


_STORE = _BanStore()


# ---------------------------------------------------------------------------
# Geolokasi (async + cache)
# ---------------------------------------------------------------------------

_GEO_CACHE = {}
_GEO_INFO = {}
_GEO_URL = "https://ipwho.is/{ip}"


def _geo_payload(ip):
    """Detail geolokasi → dict {loc, isp, org, type}; None bila tak ada."""
    try:
        if ipaddress.ip_address(ip).is_private:
            return None
    except ValueError:
        return None
    try:
        with urllib.request.urlopen(_GEO_URL.format(ip=ip), timeout=8) as r:
            j = json.loads(r.read().decode("utf-8"))
        if not j.get("success"):
            return None
        parts = [j.get("city"), j.get("region"), j.get("country")]
        return {"loc": ", ".join(str(x) for x in parts if x) or None,
                "isp": (j.get("connection") or {}).get("isp"),
                "org": (j.get("connection") or {}).get("org"),
                "type": (j.get("connection") or {}).get("type")}
    except Exception:  # noqa: BLE001
        return None


def _fetch_geo(ip):
    g = _geo_payload(ip)
    return (g or {}).get("loc")


def geo_info(ip):
    """Detail geolokasi IP (cache) → dict {loc, isp, org, type}; None bila
    tak ada. Dipakai track.py untuk data pengunjung."""
    if not ip:
        return None
    if ip not in _GEO_INFO:
        _GEO_INFO[ip] = _geo_payload(ip) or {}
    return _GEO_INFO[ip] or None


def _geo_for(ip):
    if not ip or ip in _GEO_CACHE:
        return _GEO_CACHE.get(ip)
    g = _fetch_geo(ip)
    _GEO_CACHE[ip] = g or ""
    return g


def _notify(ip, e, is_new):
    """Kirim notifikasi serangan ke owner (thread)."""
    try:
        import webdenz
        from denzbot import tg_notify
        cfg = webdenz.load_config()
        if not cfg.get("tg_notify_security", True):
            return
        if not (cfg.get("tg_bot_token") and cfg.get("tg_chat_id")):
            return
        geo = _geo_for(ip) or "-"
        with _STORE._lock:
            _STORE._ensure_loaded()
            cur = _STORE._bans.get(ip)
            if cur is not None:
                cur["geo"] = geo or cur.get("geo") or ""
                _STORE._save()
        label = "SERANGAN DIBLOKIR" if not is_new else "🚨 SERANGAN DIBLOKIR"
        text = (f"{label}\n"
                f"IP: {ip}\n"
                f"Lokasi: {geo}\n"
                f"Waktu: {e.get('last_seen')}\n"
                f"Alasan: {e.get('reason')} (total {e.get('count')}x)\n"
                f"UA: {e.get('ua') or '-'}\n"
                f"Path: {e.get('path') or '-'}\n"
                f"\nUnban: /unbanip {ip}")
        tg_notify(text)
    except Exception:  # noqa: BLE001
        pass


def ban(ip, reason, ua="", path="", method=""):
    """Ban IP PERMANEN + notifikasi. IP loopback tidak pernah di-ban."""
    ip = str(ip or "").strip()
    if not ip or _is_loopback(ip):
        return None
    e, notify = _STORE.add(ip, reason, ua, path)
    if notify:
        threading.Thread(target=_notify, args=(ip, e, True), daemon=True).start()
    return e


def is_banned(ip):
    return bool(ip) and _STORE.is_banned(str(ip))


def unban(ip):
    return _STORE.remove(str(ip).strip())


def list_bans():
    return _STORE.all()


# ---------------------------------------------------------------------------
# Pencacah (brute-force / scan) — window in-memory per (ip,kind)
# ---------------------------------------------------------------------------

_WIN = {}
_WIN_LOCK = threading.Lock()


def flag(ip, kind, reason, threshold=None, window=600):
    """Tambah hitungan kejadian mencurigakan; ban bila lewat threshold.

    Return True bila baru saja di-ban karena ini.
    """
    ip = str(ip or "").strip()
    if not ip or _is_loopback(ip):
        return False
    cfg = _load_cfg()
    if not cfg.get("waf", True):
        return False
    threshold = threshold or int(cfg.get("ban_fail_threshold") or 6)
    now = time.time()
    with _WIN_LOCK:
        key = (ip, kind)
        b = _WIN.get(key)
        if not b or now - b["t0"] > window:
            b = {"t0": now, "c": 0}
            _WIN[key] = b
        b["c"] += 1
        if b["c"] >= threshold:
            _WIN.pop(key, None)
    if b["c"] >= threshold:
        ban(ip, reason, path="")
        return True
    return False


_404WIN = {}
_404_LOCK = threading.Lock()


def record_404(ip, path, threshold=None, window=60):
    """Catat 404 ke path yang belum pernah dilihat (endpoint scan).

    Return True bila baru di-ban karenanya.
    """
    ip = str(ip or "").strip()
    if not ip or _is_loopback(ip):
        return False
    cfg = _load_cfg()
    if not cfg.get("waf", True):
        return False
    threshold = threshold or int(cfg.get("ban_scan_threshold") or 25)
    now = time.time()
    with _404_LOCK:
        b = _404WIN.get(ip)
        if not b or now - b["t0"] > window:
            b = {"t0": now, "paths": set()}
            _404WIN[ip] = b
        b["paths"].add(path)
        n = len(b["paths"])
        if n >= threshold:
            _404WIN.pop(ip, None)
    if n >= threshold:
        ban(ip, f"endpoint scan ({n} path tidak dikenal dalam {window}s)",
            path=path)
        return True
    return False


# ---------------------------------------------------------------------------
# IP asli klien (trust header proxy HANYA dari loopback)
# ---------------------------------------------------------------------------

_TRUSTED = ("CF-Connecting-IP", "X-Forwarded-For", "X-Real-IP")


def get_real_ip(peer, headers):
    """IP asli klien.

    - Bila peer adalah loopback (koneksi dari cloudflared), percaya header
      CF-Connecting-IP / X-Forwarded-For (ambil yang paling kiri).
    - Selain itu, header tidak dipercaya (cegah spoof dari akses langsung).
    """
    peer_ip = str(peer[0] if isinstance(peer, (tuple, list)) else peer)
    if _is_loopback(peer_ip):
        for h in _TRUSTED:
            v = (headers or {}).get(h)
            if v:
                cand = str(v).split(",")[0].strip()
                if cand and cand.lower() != "unknown":
                    return cand
    return peer_ip


def is_loopback(ip):
    return _is_loopback(ip)
