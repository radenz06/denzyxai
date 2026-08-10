#!/usr/bin/env python3
"""auto-denz — daemon headless denzyx AI (hidup 24 jam).

Bekerja tanpa TUI/terminal: polling notifikasi Android, deteksi yang BARU,
dibalas otomatis oleh AI (dikirim balik sebagai notifikasi + opsional TTS),
plus monitor baterai low. Semua kejadian dicatat di log.

Mode:
  python3 auto-denz.py run        # jalankan daemon (loop utama)
  python3 auto-denz.py once       # jalankan 1 siklus lalu keluar (untuk tes)
  python3 auto-denz.py ensure     # pastikan daemon jalan; kalau mati, start
  python3 auto-denz.py install    # setup: job-scheduler + boot + wake-lock
  python3 auto-denz.py stop       # hentikan daemon (job tetap ada)
  python3 auto-denz.py uninstall  # hapus job scheduler
  python3 auto-denz.py status     # info daemon + konfigurasi

Konfigurasi via env:
  DENZYX_AUTO_INTERVAL        detik antar polling        (default 30)
  DENZYX_AUTO_AI              balas via AI 0/1           (default 1)
  DENZYX_AUTO_REPLY_NOTIF     kirim balasan sbg notif    (default 1)
  DENZYX_AUTO_TTS             suarakan balasan 0/1       (default 0)
  DENZYX_AUTO_VIBRATE         getar saat notif baru      (default 1)
  DENZYX_AUTO_BATTERY_LOW     ambang baterai alert %    (default 20)
  DENZYX_AUTO_IGNORE_APPS     paket yang diabaikan       (default com.termux,
                             com.android.systemui,com.android.settings,
                             com.android.vending)
  DENZYX_AUTO_TTL             detik baru dianggap "baru" (default 43200=12 jam)
  DENZYX_AUTO_LOG             path log                   (default ~/.denzyx_auto.log)
"""

import json
import os
import queue
import socket
import subprocess
import sys
import time
from collections import deque
from datetime import datetime, timedelta
from pathlib import Path

APP_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(APP_DIR))
import denzyx as app  # reuse State + _api_stream (bukan TUI)

# Semua file state dianchor ke home Termux (= parent proyek), bukan $HOME,
# supaya konsisten di perangkat & saat sandbox (HOME bisa beda).
TERMUX_HOME = APP_DIR.parent
PID_FILE = TERMUX_HOME / ".denzyx_auto.pid"
JOB_ID = "745"
BOOT_DIR = TERMUX_HOME / ".termux/boot"


def _env(name, default):
    return os.environ.get(name, default)


def _envint(name, default):
    try:
        return int(os.environ.get(name, default))
    except (TypeError, ValueError):
        return default


def _envbool(name, default):
    v = os.environ.get(name)
    if v is None:
        return default
    return v.strip().lower() in ("1", "true", "yes", "on")


INTERVAL = _envint("DENZYX_AUTO_INTERVAL", 30)
AI_REPLY = _envbool("DENZYX_AUTO_AI", True)
REPLY_NOTIF = _envbool("DENZYX_AUTO_REPLY_NOTIF", True)
TTS = _envbool("DENZYX_AUTO_TTS", False)
VIBRATE = _envbool("DENZYX_AUTO_VIBRATE", True)
BATTERY_LOW = _envint("DENZYX_AUTO_BATTERY_LOW", 20)
IGNORE_APPS = set(p.strip() for p in
                  _env("DENZYX_AUTO_IGNORE_APPS",
                       "com.termux,com.termux.api,com.android.systemui,"
                       "com.android.settings,com.android.vending").split(",")
                  if p.strip())
# isi notif yang nggak perlu dibalas (sensitif disembunyikan / kosong)
SKIP_TAIL = ("Konten notifikasi sensitif disembunyikan", "Sensitive content",
             "hidden because of sensitive content")
TTL = _envint("DENZYX_AUTO_TTL", 43200)
LOG_FILE = Path(os.path.expanduser(_env("DENZYX_AUTO_LOG", ".denzyx_auto.log")))
if not LOG_FILE.is_absolute():
    LOG_FILE = TERMUX_HOME / LOG_FILE


def log(msg):
    line = f"{datetime.now():%Y-%m-%d %H:%M:%S} {msg}"
    try:
        LOG_FILE.parent.mkdir(parents=True, exist_ok=True)
        with LOG_FILE.open("a", encoding="utf-8") as f:
            f.write(line + "\n")
    except OSError:
        pass
    print(line, flush=True)


def run_dev(argv, timeout=15, stdin=None):
    try:
        proc = subprocess.run(argv, input=stdin, capture_output=True,
                              text=True, timeout=timeout)
        out = (proc.stdout or "").strip()
        err = (proc.stderr or "").strip()
        return out or err
    except Exception as e:  # noqa: BLE001
        return f"error: {e}"


# ---------------------------------------------------------------------------
# Deteksi koneksi internet (auto resume saat online lagi)
# ---------------------------------------------------------------------------

_online_cache = {"t": 0.0, "v": None}


def _api_host():
    try:
        from urllib.parse import urlparse
        return urlparse(app.State().url).hostname or "opencode.ai"
    except Exception:  # noqa: BLE001
        return "opencode.ai"


def _is_online(timeout=3):
    """Cek apakah host API AI bisa dijangkau. Hasil di-cache 5 detik biar
    nggak nge-probe tiap siklus."""
    now = time.time()
    if now - _online_cache["t"] < 5 and _online_cache["v"] is not None:
        return _online_cache["v"]
    try:
        sock = socket.create_connection((_api_host(), 443), timeout=timeout)
        sock.close()
        ok = True
    except OSError:
        ok = False
    _online_cache.update(t=now, v=ok)
    return ok


# ---------------------------------------------------------------------------
# Pembacaan perangkat
# ---------------------------------------------------------------------------

def get_notifications():
    raw = run_dev(["termux-notification-list"], timeout=15)
    try:
        data = json.loads(raw)
        return data if isinstance(data, list) else []
    except (ValueError, TypeError):
        if "permission" in raw.lower() or "grant" in raw.lower():
            log("! butuh izin notifikasi: Settings → Apps → Special access → "
                "Notification access → aktifkan Termux:API")
        return []


def battery_pct():
    raw = run_dev(["termux-battery-status"], timeout=15)
    try:
        data = json.loads(raw)
        return data.get("percentage")
    except (ValueError, TypeError):
        return None


def notif_key(n):
    """Kunci identitas notif = paket + judul + pesan terakhir (baris preview
    terbaru). Kalau ada pesan baru, baris terakhir berubah → dianggap baru."""
    lines = n.get("lines") or []
    tail = (lines[-1] if lines else "").strip()
    if not tail:
        tail = (n.get("content") or "").strip()
    return f"{n.get('packageName', '')}|{n.get('title', '')}|{tail}"


# ---------------------------------------------------------------------------
# Aksi
# ---------------------------------------------------------------------------

def notify_reply(text, title="⚡ Denz"):
    run_dev(["termux-notification", "-i", "denz_auto", "-t", title,
             "-c", text], timeout=15)


def speak(text):
    run_dev(["termux-tts-speak", text], timeout=30)


def vibrate():
    run_dev(["termux-vibrate", "-d", "400"], timeout=10)


def ask_ai(state, prompt):
    """Minta AI balas. Return teks balasan, atau None kalau offline/gagal
    (pemanggil akan mengantre ulang)."""
    if not _is_online():
        return None
    msgs = []
    sysp = app.load_system_prompt()
    if sysp:
        msgs.append({"role": "system", "content": sysp})
    msgs.append({"role": "system", "content":
                 "Kamu lagi mode AUTO (headless, tanpa TUI). Balas SINGKAT "
                 "maksimal 2 kalimat, gaya Jaksel sarkastik, to the point, "
                 "langsung ke inti. Tanpa basa-basi."})
    msgs.append({"role": "user", "content": prompt})
    q = queue.Queue()
    try:
        content, reasoning, _calls = app._api_stream(
            state, msgs, None, q, timeout=45, visible=False)
        return (content or reasoning or "").strip()
    except Exception as e:  # noqa: BLE001
        log(f"! AI gagal: {e}")
        return None


# ---------------------------------------------------------------------------
# Inti
# ---------------------------------------------------------------------------

def try_reply(state, n):
    """Balas 1 notifikasi. Return True kalau berhasil, False kalau harus
    diantre ulang (offline / gagal)."""
    pkg = n.get("packageName", "")
    title = n.get("title", "")
    lines = n.get("lines") or []
    tail = (lines[-1] if lines else "").strip() or (n.get("content") or "").strip()
    log(f"NOTIF baru dari {pkg} | {title}: {tail[:200]}")
    if VIBRATE:
        vibrate()
    if not AI_REPLY:
        return True
    if not _is_online():
        log("  → offline, antre untuk dibalas saat online")
        return False
    prompt = (f"Ada notifikasi baru di HP:\n"
              f"- paket: {pkg}\n- judul: {title}\n- isi: {tail[:500]}\n\n"
              f"Balas singkat (maks 2 kalimat) sebagai Denz, gaya Jaksel "
              f"sarkastik, to the point.")
    reply = ask_ai(state, prompt)
    if not reply:
        return False
    log(f"BALASAN Denz: {reply[:200]}")
    if REPLY_NOTIF:
        notify_reply(reply)
    if TTS:
        speak(reply)
    return True


def run_loop(state, once=False):
    seen = {}
    battery_warned = False
    was_online = None
    pending = deque()
    log(f"auto-denz mulai | interval={INTERVAL}s AI={AI_REPLY} "
        f"notif={REPLY_NOTIF} tts={TTS} baterai_low={BATTERY_LOW}%")
    run_dev(["termux-wake-lock"], timeout=10)
    while True:
        try:
            online = _is_online()
            if was_online is not None and online != was_online:
                if online:
                    log(f"koneksi kembali — proses antrian {len(pending)} notif")
                else:
                    log("koneksi putus — AI dijeda, notif diantre untuk dibalas "
                        "saat online")
            was_online = online

            pct = battery_pct()
            if pct is not None:
                if pct <= BATTERY_LOW and not battery_warned:
                    battery_warned = True
                    msg = f"Baterai {pct}% — low! Colok charger sekarang."
                    log("ALERT " + msg)
                    notify_reply(msg, title="🔋 Denz")
                    if TTS:
                        speak(msg)
                elif pct > BATTERY_LOW + 10:
                    battery_warned = False

            now = time.time()
            for k in [k for k, t in seen.items() if now - t > TTL]:
                del seen[k]
            for n in get_notifications():
                pkg = n.get("packageName", "")
                if pkg in IGNORE_APPS:
                    continue
                k = notif_key(n)
                if not k or k in seen:
                    continue
                seen[k] = now
                lines = n.get("lines") or []
                tail = (lines[-1] if lines else "").strip() or \
                    (n.get("content") or "").strip()
                if not tail or any(t in tail for t in SKIP_TAIL):
                    continue
                pending.append((n, 0))

            if len(pending) > 50:
                for _ in range(len(pending) - 50):
                    pending.popleft()

            if online:
                done = 0
                while pending and done < 3:
                    n, tries = pending.popleft()
                    if not _is_online():
                        pending.appendleft((n, tries))
                        break
                    if tries >= 3:
                        log(f"! notif gagal dijawab 3x, dilepas "
                            f"({n.get('packageName', '')})")
                        continue
                    if try_reply(state, n):
                        done += 1
                    else:
                        pending.append((n, tries + 1))
                        break
        except Exception as e:  # noqa: BLE001
            log(f"error siklus: {e}")
        if once:
            break
        time.sleep(INTERVAL)


# ---------------------------------------------------------------------------
# Manajemen daemon
# ---------------------------------------------------------------------------

def is_running():
    if not PID_FILE.exists():
        return False
    try:
        pid = int(PID_FILE.read_text().strip())
        os.kill(pid, 0)
        return True
    except (ValueError, OSError, ProcessLookupError):
        return False


def start_daemon():
    if is_running():
        log("daemon sudah jalan.")
        return
    py = sys.executable or "python3"
    script = str(APP_DIR / "auto-denz.py")
    logfile = str(LOG_FILE)
    cmd = f"nohup {py} '{script}' run >>'{logfile}' 2>&1 &"
    subprocess.run(cmd, shell=True, cwd=str(APP_DIR))
    time.sleep(1)
    log(f"daemon di-start (pid lihat di {PID_FILE}).")


def cmd_install():
    script = str(APP_DIR / "auto-denz.py")
    # path proyek bisa mengandung spasi ("denzyx ai") → job scheduler perlu
    # path tanpa spasi; termux-api HANYA menerima script di dalam home
    # Termux (/data/data/com.termux/files/home) → taruh wrapper di sini.
    wrapper = APP_DIR.parent / ".denzyx_auto_job.sh"
    sh = "/data/data/com.termux/files/usr/bin/sh"
    wrapper.write_text(
        f"#!{sh}\n# wrapper job scheduler auto-denz (path tanpa spasi)\n"
        f"exec {sys.executable or 'python3'} {script} ensure\n",
        encoding="utf-8")
    wrapper.chmod(0o755)
    log(f"job wrapper: {wrapper}")
    log("install: mendaftarkan job scheduler (respawn tiap 15 menit)...")
    run_dev(["termux-job-scheduler", "-s", str(wrapper), "--job-id", JOB_ID,
             "--period-ms", "900000", "--persisted", "true",
             "--battery-not-low", "true"], timeout=15)
    try:
        BOOT_DIR.mkdir(parents=True, exist_ok=True)
        boot = BOOT_DIR / "denz.sh"
        boot.write_text(
            f"#!/data/data/com.termux/files/usr/bin/sh\n"
            f"# auto-start denzyx AI daemon saat HP reboot (butuh termux-boot)\n"
            f"{sys.executable or 'python3'} {script} ensure\n", encoding="utf-8")
        boot.chmod(0o755)
        log(f"boot script: {boot} (install package termux-boot biar dipakai)")
    except OSError as e:
        log(f"! gagal buat boot script: {e}")
    run_dev(["termux-wake-lock"], timeout=10)
    log("wake-lock aktif. Nonaktifkan battery optimization Termux di "
        "Android settings biar proses nggak dibunuh.")
    log("install selesai. Jalankan 'auto-denz.py run' sekali manual buat tes.")


def cmd_uninstall():
    run_dev(["termux-job-scheduler", "--cancel-all"], timeout=15)
    log("job scheduler dicancel.")
    cmd_stop()


def cmd_stop():
    if not PID_FILE.exists():
        log("daemon tidak sedang jalan (tidak ada pid file).")
        return
    try:
        pid = int(PID_FILE.read_text().strip())
        os.kill(pid, 9)
        log(f"daemon (pid {pid}) dihentikan.")
    except (ValueError, OSError) as e:
        log(f"! gagal hentikan daemon: {e}")
    PID_FILE.unlink(missing_ok=True)


def cmd_status():
    print("─ auto-denz status ─")
    print(f"daemon jalan  : {'YA' if is_running() else 'TIDAK'} "
          f"(pid file: {PID_FILE})")
    print(f"interval      : {INTERVAL}s")
    print(f"balas AI      : {AI_REPLY} | notif: {REPLY_NOTIF} | tts: {TTS}")
    print(f"baterai alert : {BATTERY_LOW}% | vibrate: {VIBRATE}")
    print(f"ignore apps   : {', '.join(sorted(IGNORE_APPS)) or '(kosong)'}")
    print(f"log           : {LOG_FILE}")
    jobs = run_dev(["termux-job-scheduler", "--pending"], timeout=10)
    print("─ job scheduler ─")
    print(jobs[:500] if jobs else "(tidak ada job / gagal baca)")
    if LOG_FILE.exists():
        tail = "\n".join(LOG_FILE.read_text(errors="replace")
                         .splitlines()[-10:])
        print("─ log terakhir ─")
        print(tail)


def main():
    cmd = sys.argv[1] if len(sys.argv) > 1 else "run"
    if cmd == "run":
        if PID_FILE.exists() and is_running():
            log("daemon lain sudah jalan — keluar.")
            return
        PID_FILE.write_text(str(os.getpid()), encoding="utf-8")
        try:
            run_loop(app.State(), once=False)
        finally:
            try:
                if PID_FILE.exists():
                    PID_FILE.unlink()
            except OSError:
                pass
    elif cmd == "once":
        run_loop(app.State(), once=True)
    elif cmd == "ensure":
        start_daemon()
    elif cmd == "install":
        cmd_install()
    elif cmd == "stop":
        cmd_stop()
    elif cmd == "uninstall":
        cmd_uninstall()
    elif cmd == "status":
        cmd_status()
    else:
        print(__doc__)


if __name__ == "__main__":
    try:
        import lic
        lic.require()
    except SystemExit:
        raise
    except Exception:  # noqa: BLE001
        pass
    main()
