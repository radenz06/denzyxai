#!/usr/bin/env python3
"""track — perekam lengkap pengunjung web denzyx.

Untuk setiap request, kita kumpulkan sebanyak mungkin informasi visitor:
- IP public & private: peer socket, header CF-Connecting-IP, rantai
  X-Forwarded-For / X-Real-IP (hanya dipercaya dari koneksi loopback, sama
  seperti waf.get_real_ip — cegah spoof), diklasifikasikan
  public/private/loopback.
- Lokasi & ISP: geolokasi IP publik via ipwho.is (async + cache, pakai
  infra waf.geo_info).
- Software: User-Agent di-parse → browser, OS, tipe perangkat, engine, bot.
- Perilaku: path yang dikunjungi, metode, referer, status, waktu.

Penyimpanan:
- webdata/visitors.json     — agregat per IP (dipakai owner panel + CLI).
- webdata/logs/visitors.log — riwayat per kunjungan (dibatasi 1 baris/menit/IP).

Semua operasi thread-safe & non-blocking (geo di thread background; file
ditulis atomik, flush dibatasi 10 detik).
"""

import ipaddress
import json
import os
import re
import threading
import time
from pathlib import Path

_VISITORS = {}
_LOCK = threading.Lock()
_last_flush = 0.0
_last_log = {}
_GEO_FLAG = True


def _cipher():
    """Cipher Fernet lokal (satu key dgn webconfig — webdata/.config.key)."""
    import securecfg
    from cryptography.fernet import Fernet
    return Fernet(securecfg.cfg_key())


def _enc(text):
    """Enkripsi string → token Fernet ascii. Gagal → fallback plaintext."""
    try:
        return _cipher().encrypt(text.encode("utf-8")).decode("ascii")
    except Exception:  # noqa: BLE001
        return text


def _dec(text):
    """Dekripsi token Fernet → string. Bukan token (legacy) → return apa adanya."""
    try:
        return _cipher().decrypt(text.encode("utf-8")).decode("utf-8")
    except Exception:  # noqa: BLE001
        return text


def _base():
    return Path(os.environ.get("WEBDENZ_DATA")
                or Path(__file__).resolve().parent / "webdata")


def visitors_file():
    return _base() / "visitors.json"


def set_geo(enabled):
    """Aktifkan/matikan geolokasi (default: aktif)."""
    global _GEO_FLAG
    _GEO_FLAG = bool(enabled)


def _geo_ok():
    env = os.environ.get("WEBDENZ_TRACK_GEO")
    if env is not None:
        return env != "0"
    return _GEO_FLAG


# ---------------------------------------------------------------------------
# Klasifikasi IP
# ---------------------------------------------------------------------------

def ip_class(ip):
    """'public' | 'private' | 'loopback' | 'invalid'."""
    ip = str(ip or "").strip()
    if not ip:
        return "invalid"
    try:
        a = ipaddress.ip_address(ip.split("%", 1)[0])
    except ValueError:
        return "invalid"
    if a.is_loopback:
        return "loopback"
    if a.is_private or a.is_reserved or a.is_link_local:
        return "private"
    return "public"


# ---------------------------------------------------------------------------
# Parser User-Agent (tanpa dependency)
# ---------------------------------------------------------------------------

_BOT_RE = re.compile(
    r"bot|crawl|spider|slurp|facebookexternalhit|whatsapp|"
    r"curl|wget|python-requests|python-urllib|go-http-client|okhttp|httpx|"
    r"java/|libwww|postman|nmap|nikto|sqlmap|masscan|zgrab|headless|"
    r"lighthouse|pingdom|uptimerobot|monitor", re.I)

_OS_PAT = [
    (r"windows nt 10\.0|windows nt 6\.4", "Windows 10/11"),
    (r"windows nt 6\.3", "Windows 8.1"),
    (r"windows nt 6\.2", "Windows 8"),
    (r"windows nt 6\.1", "Windows 7"),
    (r"windows nt 6\.0", "Windows Vista"),
    (r"windows nt 5\.[12]", "Windows XP"),
    (r"windows phone", "Windows Phone"),
    (r"iphone|ipad|ipod|cfnetwork", "iOS"),
    (r"mac os x|macintosh", "macOS"),
    (r"android", "Android"),
    (r"cros", "ChromeOS"),
    (r"ubuntu", "Ubuntu"),
    (r"freebsd", "FreeBSD"),
    (r"debian", "Debian"),
    (r"linux", "Linux"),
    (r"windows", "Windows"),
]


def parse_ua(ua):
    """Parse User-Agent → browser, os, device, engine, is_bot, label."""
    ua = (ua or "").strip()
    info = {"ua": ua, "browser": "-", "os": "-", "device": "desktop",
            "engine": "-", "is_bot": False, "label": "-"}
    if not ua:
        info["device"] = "-"
        return info
    low = ua.lower()
    info["is_bot"] = bool(_BOT_RE.search(low))

    for pat, name in _OS_PAT:
        if re.search(pat, low):
            info["os"] = name
            break

    if re.search(r"\bopr\b|opera mini|opios", low):
        info["browser"] = "Opera"
    elif "edg/" in low or "edge/" in low:
        info["browser"] = "Edge"
    elif "samsungbrowser" in low:
        info["browser"] = "Samsung Internet"
    elif "vivaldi" in low:
        info["browser"] = "Vivaldi"
    elif "miuibrowser" in low:
        info["browser"] = "Miui Browser"
    elif "ucbrowser" in low:
        info["browser"] = "UC Browser"
    elif "brave" in low:
        info["browser"] = "Brave"
    elif "firefox" in low:
        info["browser"] = "Firefox"
    elif "chrome" in low or "chromium" in low or "crios" in low:
        info["browser"] = "Chrome"
    elif "safari" in low:
        info["browser"] = "Safari"
    elif "curl" in low:
        info["browser"] = "curl"
    elif "wget" in low:
        info["browser"] = "wget"
    elif "python-requests" in low:
        info["browser"] = "python-requests"
    elif info["is_bot"]:
        tool = re.search(r"\b(sqlmap|nikto|nmap|masscan|zgrab|gobuster|"
                         r"dirsearch|ffuf|hydra|wpscan|burpsuite|"
                         r"python-requests|python-urllib|go-http-client|"
                         r"okhttp|libwww|curl|wget|scrapy|headlesschrome)\b",
                         low)
        if tool:
            info["browser"] = tool.group(1)
        else:
            m = re.search(r"[a-z0-9_-]*(?:bot|spider|crawl)[a-z0-9_-]*", low)
            info["browser"] = m.group(0) if m else "bot"
    else:
        info["browser"] = "?"

    if info["is_bot"]:
        info["device"] = "bot"
    elif re.search(r"ipad|tablet|playbook|kindle|silkt", low):
        info["device"] = "tablet"
    elif re.search(r"mobile|iphone|ipod|opera mini", low):
        info["device"] = "mobile"
    else:
        info["device"] = "desktop"

    if "presto" in low:
        info["engine"] = "Presto"
    elif "trident" in low:
        info["engine"] = "Trident"
    elif "gecko" in low and "webkit" not in low:
        info["engine"] = "Gecko"
    elif "webkit" in low:
        info["engine"] = "WebKit"

    ver = re.search(r"(?:chrome|edg|firefox|opr|samsungbrowser|vivaldi)"
                    r"/([0-9.]+)", low)
    info["label"] = info["browser"] + (f" {ver.group(1)}" if ver else "")
    return info


# ---------------------------------------------------------------------------
# Rekam kunjungan
# ---------------------------------------------------------------------------

def visit(ip, headers=None, path="", method="GET", peer=None, status=0):
    """Rekam satu kunjungan. Non-blocking; geo di thread background.

    headers: dict header request (User-Agent, CF-Connecting-IP, dll).
    peer: alamat socket (tuple (ip, port)) atau string IP.
    """
    ip = str(ip or "").strip()
    if not ip:
        return
    headers = headers or {}
    path = (path or "/").split("?", 1)[0] or "/"
    method = str(method or "GET").upper()
    now = time.time()
    iso = time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(now))
    ua = headers.get("User-Agent", "")
    parsed = parse_ua(ua)
    peer_ip = str(peer[0] if isinstance(peer, (tuple, list)) else (peer or ""))
    cf_ip = str(headers.get("CF-Connecting-IP") or "").split(",")[0].strip()
    xff = str(headers.get("X-Forwarded-For") or "").strip()
    ref = str(headers.get("Referer") or "")[:250]
    lang = str(headers.get("Accept-Language") or "")[:120].strip()
    dnt = str(headers.get("DNT") or "").strip()
    hints = {}
    for h in ("Sec-CH-UA", "Sec-CH-UA-Platform", "Sec-CH-UA-Platform-Version",
              "Sec-CH-UA-Arch", "Sec-CH-UA-Model", "Sec-CH-UA-Mobile",
              "Sec-CH-UA-Full-Version-List"):
        val = str(headers.get(h) or "").strip()
        if val and val != "?":
            hints[h] = val[:200]
    now = time.time()

    with _LOCK:
        v = _VISITORS.get(ip)
        if v is None:
            v = _VISITORS[ip] = {
                "ip": ip,
                "ip_class": ip_class(ip),
                "peer": peer_ip,
                "cf_ip": cf_ip,
                "xff": xff,
                "geo": "", "isp": "", "org": "", "conn_type": "",
                "ua": ua, "browser": parsed["browser"], "os": parsed["os"],
                "device": parsed["device"], "engine": parsed["engine"],
                "is_bot": parsed["is_bot"],
                "lang": lang, "dnt": dnt, "client_hints": dict(hints),
                "screen": "", "tz": "", "mem_gb": "", "cpu_cores": "",
                "battery": "", "login_events": [], "last_login": "",
                "last_login_user": "", "last_fail_user": "", "last_fail": "",
                "first_seen": iso, "last_seen": iso, "visits": 0,
                "methods": {}, "statuses": {}, "paths": [],
                "referers": [], "flagged": False,
            }
        v["last_seen"] = iso
        v["visits"] += 1
        v["methods"][method] = v["methods"].get(method, 0) + 1
        if status:
            v["statuses"][str(status)] = v["statuses"].get(str(status), 0) + 1
        if path not in v["paths"]:
            v["paths"].append(path)
            v["paths"] = v["paths"][-20:]
        if ref and ref not in v["referers"]:
            v["referers"].append(ref)
            v["referers"] = v["referers"][-5:]
        if ua and not v["ua"]:
            v["ua"] = ua
        if lang and not v.get("lang"):
            v["lang"] = lang
        if dnt and not v.get("dnt"):
            v["dnt"] = dnt
        for h, val in hints.items():
            if h not in v.get("client_hints", {}):
                v.setdefault("client_hints", {})[h] = val
        needs_geo = _geo_ok() and not v.get("geo")
        log_now = now - _last_log.get(ip, 0.0) >= 60.0
        if log_now:
            _last_log[ip] = now

    if log_now:
        _append_log(v, iso, path, method, ref)
    if needs_geo:
        threading.Thread(target=_fill_geo, args=(ip,), daemon=True).start()
    _maybe_flush(now)


def status(ip, code):
    """Rekam kode status response (dipanggil dari _send, per request)."""
    ip = str(ip or "").strip()
    if not ip:
        return
    with _LOCK:
        v = _VISITORS.get(ip)
        if v is None:
            return
        v["statuses"][str(code)] = v["statuses"].get(str(code), 0) + 1


def ping(ip, info=None):
    """Perbarui detail perangkat dari browser (beacon /api/ping).

    info dict: screen (mis. "390x844"), dpr, tz (mis. "Asia/Jakarta"),
    tz_offset, mem_gb, cpu_cores, battery, lang, device_memory, dll.
    """
    ip = str(ip or "").strip()
    if not ip:
        return
    info = info or {}
    now = time.time()
    with _LOCK:
        v = _VISITORS.get(ip)
        if v is None:
            return
        if info.get("screen"):
            v["screen"] = str(info["screen"])[:40]
        if info.get("tz"):
            v["tz"] = str(info["tz"])[:64]
        if info.get("mem_gb"):
            v["mem_gb"] = str(info["mem_gb"])[:16]
        if info.get("cpu_cores"):
            v["cpu_cores"] = str(info["cpu_cores"])[:8]
        if info.get("battery"):
            v["battery"] = str(info["battery"])[:16]
        if info.get("lang") and not v.get("lang"):
            v["lang"] = str(info["lang"])[:120]
    _maybe_flush(now)


def login(ip, username, ok):
    """Rekam percobaan login (sukses/gagal) ke record visitor IP ini."""
    ip = str(ip or "").strip()
    if not ip:
        return
    now = time.time()
    iso = time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(now))
    username = str(username or "?")[:60]
    with _LOCK:
        v = _VISITORS.get(ip)
        if v is None:
            v = _VISITORS[ip] = {
                "ip": ip, "ip_class": ip_class(ip), "peer": "", "cf_ip": "",
                "xff": "", "geo": "", "isp": "", "org": "", "conn_type": "",
                "ua": "", "browser": "-", "os": "-", "device": "-",
                "engine": "-", "is_bot": False, "lang": "", "dnt": "",
                "client_hints": {}, "screen": "", "tz": "", "mem_gb": "",
                "cpu_cores": "", "battery": "", "login_events": [],
                "last_login": "", "last_login_user": "",
                "last_fail_user": "", "last_fail": "",
                "first_seen": iso, "last_seen": iso, "visits": 0,
                "methods": {}, "statuses": {}, "paths": [],
                "referers": [], "flagged": False,
            }
        events = v.get("login_events") or []
        events.append({"user": username, "ok": bool(ok), "ts": iso})
        v["login_events"] = events[-30:]
        if ok:
            v["last_login"] = iso
            v["last_login_user"] = username
        else:
            v["last_fail"] = iso
            v["last_fail_user"] = username
    _maybe_flush(now)


def _fill_geo(ip):
    try:
        import waf
        g = waf.geo_info(ip) or {}
    except Exception:  # noqa: BLE001
        g = {}
    with _LOCK:
        v = _VISITORS.get(ip)
        if v is None:
            return
        v["geo"] = g.get("loc") or ""
        v["isp"] = g.get("isp") or ""
        v["org"] = g.get("org") or ""
        v["conn_type"] = g.get("type") or ""
    _flush_force()


def _append_log(v, iso, path, method, ref):
    try:
        base = _base() / "logs"
        base.mkdir(parents=True, exist_ok=True)
        row = {"ts": iso, "ip": v["ip"], "ip_class": v["ip_class"],
               "peer": v["peer"], "cf_ip": v["cf_ip"], "xff": v["xff"],
               "path": path, "method": method, "ref": ref,
               "geo": v["geo"], "isp": v["isp"], "org": v["org"],
               "browser": v["browser"], "os": v["os"], "device": v["device"],
               "is_bot": v["is_bot"], "ua": v["ua"]}
        with open(base / "visitors.log", "a", encoding="utf-8") as fh:
            fh.write(_enc(json.dumps(row, ensure_ascii=False)) + "\n")
    except OSError:
        pass


def _write(data):
    try:
        base = _base()
        base.mkdir(parents=True, exist_ok=True)
        tmp = base / "visitors.json.tmp"
        tmp.write_text(
            _enc(json.dumps(data, ensure_ascii=False, indent=2)),
            encoding="utf-8")
        tmp.replace(base / "visitors.json")
        try:
            os.chmod(base / "visitors.json", 0o600)
        except OSError:
            pass
    except OSError:
        pass


def _maybe_flush(now):
    global _last_flush
    with _LOCK:
        if now - _last_flush < 10.0:
            return
        _last_flush = now
        data = {k: dict(v) for k, v in _VISITORS.items()}
    _write(data)


def _flush_force():
    with _LOCK:
        data = {k: dict(v) for k, v in _VISITORS.items()}
    _write(data)


def flush():
    """Paksa tulis state ke disk (dipakai sebelum baca dari file)."""
    _flush_force()


# ---------------------------------------------------------------------------
# API baca
# ---------------------------------------------------------------------------

def load():
    """Baca semua visitor → dict {ip: {...}}.

    Gabungkan data tersimpan (disk) dengan state terbaru di memori
    (memori menang), jadi hasil selalu fresh walau belum di-flush.
    """
    data = {}
    try:
        raw = visitors_file().read_text(encoding="utf-8")
        j = json.loads(_dec(raw))
        if isinstance(j, dict):
            data = j
    except (OSError, json.JSONDecodeError, TypeError):
        pass
    with _LOCK:
        for k, v in _VISITORS.items():
            data[k] = dict(v)
    return data


def get(ip):
    return load().get(str(ip or "").strip())


def recent(ip, n=30):
    """Riwayat kunjungan terbaru dari visitors.log untuk satu IP."""
    ip = str(ip or "").strip()
    try:
        lines = (_base() / "logs" / "visitors.log").read_text(
            encoding="utf-8", errors="replace").splitlines()
    except OSError:
        return []
    out = []
    for ln in reversed(lines):
        try:
            j = json.loads(_dec(ln))
        except (json.JSONDecodeError, TypeError):
            continue
        if j.get("ip") == ip:
            out.append(j)
            if len(out) >= n:
                break
    return out


def summary():
    """Ringkasan untuk owner panel / CLI."""
    data = load()
    today = time.strftime("%Y-%m-%d")
    day_ago = time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(time.time() - 86400))
    if not data:
        return {"total": 0, "today": 0, "active_24h": 0, "visits": 0,
                "bots": 0, "mobile": 0}
    vals = data.values()
    return {
        "total": len(data),
        "today": sum(1 for v in vals if str(v.get("first_seen", ""))[:10] == today),
        "active_24h": sum(1 for v in data.values()
                          if str(v.get("last_seen", "")) >= day_ago),
        "visits": sum(int(v.get("visits") or 0) for v in data.values()),
        "bots": sum(1 for v in data.values() if v.get("is_bot")),
        "mobile": sum(1 for v in data.values() if v.get("device") == "mobile"),
    }


def clear():
    """Hapus semua data visitor (memori + file)."""
    global _last_flush
    with _LOCK:
        _VISITORS.clear()
        _last_log.clear()
        _last_flush = time.time()
    try:
        visitors_file().unlink(missing_ok=True)
        (_base() / "logs" / "visitors.log").unlink(missing_ok=True)
    except OSError:
        pass


def _seed():
    """Muat data tersimpan ke memori saat module dimuat.

    Mencegah flush() dari proses baru menimpa agregat lama dengan {} —
    jadi restart server TIDAK menghilangkan data pengunjung.
    """
    try:
        raw = visitors_file().read_text(encoding="utf-8")
        j = json.loads(_dec(raw))
        if isinstance(j, dict):
            with _LOCK:
                for k, v in j.items():
                    _VISITORS.setdefault(k, v)
    except (OSError, json.JSONDecodeError, TypeError):
        pass


_seed()
