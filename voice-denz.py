#!/usr/bin/env python3
"""voice-denz.py — panggilan suara ke denzyx AI.

Dengar pakai termux-speech-to-text (STT on-device Android, tanpa server),
jawab disuarakan dengan SUARA CEWE: edge-tts voice id-ID-GadisNeural
(neural, natural — gratis, butuh internet). Kalau edge-tts gagal,
fallback ke termux-tts-speak bahasa Indonesia (suara ikut pengaturan
TTS Android, di Google TTS biasanya cewe). Percakapan disimpan ke
sessions/ biar muncul di riwayat TUI. Reuse mesin chat dari denzyx.py
(retry, fallback key, persona dari system_prompt.md).

Dependency tambahan (opsional, sangat disarankan):
    pip install edge-tts

Cara pakai:
    python3 voice-denz.py                 # mode call: terus dengar
    python3 voice-denz.py --listen-once   # dengar sekali, terus keluar
    python3 voice-denz.py --lang id-ID    # bahasa STT (default sistem)
    python3 voice-denz.py --voice-name id-ID-GadisNeural   # pilih suara
    python3 voice-denz.py --engine android                 # paksa TTS Android
    python3 voice-denz.py --no-tts        # tanpa suara, cuma teks
    python3 voice-denz.py --wake denz     # cuma respons kalau kata kunci
Bilang "stop" / "matikan" buat menutup panggilan.
"""

import argparse
import glob
import os
import queue
import re
import shutil
import subprocess
import sys
import threading
import time
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import denzyx  # noqa: E402

EXIT_WORDS = ("stop", "matikan", "putus", "selesai", "keluar", "bye",
              "exit", "tutup", "sampai jumpa", "udahan", "sudah")

# Folder TTS harus bisa dibaca app Termux (bukan /root dsb).
TERMUX_HOME = "/data/data/com.termux/files/home"
AUDIO_DIR = os.environ.get("DENZYX_TTS_DIR") or os.path.join(
    TERMUX_HOME, ".denzyx", "tts")
DEFAULT_VOICE = os.environ.get("DENZYX_TTS_VOICE") or "id-ID-GadisNeural"


def _which(cmd):
    return shutil.which(cmd)


def _plain(text):
    """Buang markdown biar enak dibacain TTS."""
    text = re.sub(r"```.*?```", " ", text, flags=re.S)
    text = re.sub(r"\[([^\]]+)\]\([^)]+\)", r"\1", text)
    text = re.sub(r"^#{1,6}\s*", "", text, flags=re.M)
    text = re.sub(r"[`*_~]", "", text)
    text = re.sub(r"\n{3,}", "\n\n", text)
    return text.strip()


def _is_exit(text):
    low = text.strip().lower().strip(".!?")
    if low in EXIT_WORDS:
        return True
    return any(low.startswith(w) for w in ("stop", "matikan", "putus",
                                           "tutup", "sampai jumpa"))


def listen(lang=None, timeout=25):
    """Rekam 1 ucapan lewat termux-speech-to-text. Return (teks, err)."""
    exe = _which("termux-speech-to-text")
    if not exe:
        return None, "STT tidak tersedia — pkg install termux-api"
    argv = [exe]
    if lang:
        argv += ["-l", lang]
    try:
        proc = subprocess.run(argv, capture_output=True, text=True,
                              timeout=timeout)
    except subprocess.TimeoutExpired:
        return None, "TIMEOUT"
    except Exception as e:  # noqa: BLE001
        return None, str(e)
    out = (proc.stdout or "").strip()
    err = (proc.stderr or "").strip()
    if not out:
        if "permission" in err.lower():
            return None, "PERMISSION"
        return None, "EMPTY"
    return out, None


def _edge_rate(rate):
    """Ubah kecepatan float ke format edge-tts: 1.1 -> '+10%'."""
    pct = int(round((float(rate) - 1.0) * 100))
    return f"{pct:+d}%"


def _synth_mp3(text, out_path, voice, rate):
    exe = _which("edge-tts")
    if not exe:
        return None, "edge-tts belum terinstall (pip install edge-tts)"
    try:
        proc = subprocess.run(
            [exe, "--voice", voice, "--rate", _edge_rate(rate),
             "--text", text, "--write-media", out_path],
            capture_output=True, text=True, timeout=120)
    except subprocess.TimeoutExpired:
        return None, "edge-tts timeout"
    except Exception as e:  # noqa: BLE001
        return None, str(e)
    if not os.path.exists(out_path) or os.path.getsize(out_path) == 0:
        return None, (proc.stderr or "").strip()[:200] or "edge-tts gagal"
    return out_path, None


def _play_mp3(path):
    """Putar mp3 sampai habis (blocking) via termux-media-player.
    Durasi dihitung ffprobe, tidur sesuai durasi — biar mic nggak
    nangkep suara sendiri waktu mau dengar lagi."""
    player = _which("termux-media-player")
    if not player:
        return "termux-media-player tidak ada — pkg install termux-api"
    dur = 1.0
    try:
        r = subprocess.run(
            ["ffprobe", "-v", "error", "-show_entries", "format=duration",
             "-of", "default=noprint_wrappers=1:nokey=1", path],
            capture_output=True, text=True, timeout=20)
        dur = float((r.stdout or "1").strip()) or 1.0
    except Exception:  # noqa: BLE001
        pass
    try:
        subprocess.run([player, "play", path], capture_output=True, text=True,
                       timeout=30)
        time.sleep(min(dur + 0.6, 90))
        subprocess.run([player, "stop"], capture_output=True, text=True,
                       timeout=10)
    except Exception as e:  # noqa: BLE001
        return str(e)
    return ""


def _speak_android(text, rate, pitch):
    """Fallback TTS Android, bahasa id-ID (di Google TTS biasanya cewe).
    Versi termux-api beda-beda: ada yang nggak punya opsi -b (block),
    jadi tidur sesuai perkiraan durasi biar mic nggak nangkep suara."""
    exe = _which("termux-tts-speak")
    if not exe:
        return "TTS tidak tersedia — pkg install termux-api"
    est = min(max(1.0, len(text) / 14.0 / float(rate)), 30)
    argv = [exe, "-l", "id-ID", "-r", str(rate), "-p", str(pitch)]
    try:
        t0 = time.monotonic()
        proc = subprocess.run(argv, input=text, capture_output=True,
                              text=True, timeout=300)
        err = (proc.stderr or "").strip()
        if "illegal option" in err:
            argv = [exe, "-l", "id-ID"]
            subprocess.run(argv, input=text, capture_output=True, text=True,
                           timeout=300)
            err = ""
        rest = est - (time.monotonic() - t0)
        if rest > 0:
            time.sleep(min(rest, 30))
        return err
    except subprocess.TimeoutExpired:
        return "TTS timeout"
    except Exception as e:  # noqa: BLE001
        return str(e)


def _tts_cleanup():
    """Buang mp3 lama (>1 hari) di folder audio biar nggak numpuk."""
    try:
        now = time.time()
        for f in glob.glob(os.path.join(AUDIO_DIR, "denz_*.mp3")):
            if now - os.path.getmtime(f) > 86400:
                os.unlink(f)
    except Exception:  # noqa: BLE001
        pass


def speak(text, rate=1.0, pitch=1.0, voice=DEFAULT_VOICE, engine="auto"):
    """Ucapkan teks dengan SUARA CEWE: edge-tts (GadisNeural) dulu,
    gagal → TTS Android bahasa Indonesia. Blocking."""
    engine = (engine or "auto").lower()
    try:
        os.makedirs(AUDIO_DIR, exist_ok=True)
    except OSError:
        pass
    _tts_cleanup()
    if engine in ("auto", "edge"):
        path = os.path.join(AUDIO_DIR, f"denz_{int(time.time() * 1000)}.mp3")
        out, err = _synth_mp3(text, path, voice, rate)
        if out:
            play_err = _play_mp3(out)
            try:
                os.unlink(out)
            except OSError:
                pass
            if play_err:
                return play_err
            return ""
        if engine == "edge":
            return f"edge-tts: {err}"
    return _speak_android(text, rate, pitch)


def ask(state, prompt):
    """1 pertanyaan ke AI (tanpa tool), stream via thread denzyx.stream_chat."""
    q = queue.Queue()
    t = threading.Thread(target=denzyx.stream_chat, args=(state, prompt, q),
                         daemon=True)
    t.start()
    parts, error = [], None
    while True:
        try:
            kind, val = q.get(timeout=0.5)
        except queue.Empty:
            if not t.is_alive():
                break
            continue
        if kind == "content":
            parts.append(val)
        elif kind == "error":
            error = val
        elif kind == "done":
            break
    t.join(timeout=5)
    if error:
        return None, error
    return "".join(parts).strip(), None


def main():
    ap = argparse.ArgumentParser(prog="voice-denz")
    ap.add_argument("--lang", help="bahasa STT, mis. id-ID (default sistem)")
    ap.add_argument("--rate", type=float, default=1.0,
                    help="kecepatan TTS (default 1.0)")
    ap.add_argument("--pitch", type=float, default=1.0,
                    help="nada TTS 0-2 (default 1.0)")
    ap.add_argument("--no-tts", action="store_true",
                    help="tanpa suara, cuma teks di layar")
    ap.add_argument("--listen-once", action="store_true",
                    help="dengar sekali lalu tutup")
    ap.add_argument("--wake", help="kata kunci opsional, mis. 'denz'")
    ap.add_argument("--listen-timeout", type=int, default=25,
                    help="maks. detik menunggu ucapan (default 25)")
    ap.add_argument("--voice-name", default=DEFAULT_VOICE,
                    help=f"suara edge-tts (default {DEFAULT_VOICE})")
    ap.add_argument("--engine", choices=["auto", "edge", "android"],
                    default="auto",
                    help="edge = neural cewe (butuh internet), android = TTS "
                         "device, auto = coba edge dulu")
    args = ap.parse_args()

    state = denzyx.State()
    state.cwd = Path.cwd()

    def out(m="", end="\n"):
        print(m, end=end, flush=True)
    err = lambda m: print("!! " + m, file=sys.stderr, flush=True)

    def say(text):
        out("Denz: " + text)
        if not args.no_tts and text:
            msg = speak(_plain(text)[:4000], rate=args.rate,
                        pitch=args.pitch, voice=args.voice_name,
                        engine=args.engine)
            if msg and "tidak tersedia" in msg:
                err(msg)

    out("=== 📞 Panggilan suara — denzyx AI ===")
    if args.wake:
        out(f"Mode wake: sebut '{args.wake}' dulu biar direspons.")
    out(f"Suara: {args.voice_name} ({args.engine}) — bilang \"stop\" buat "
        "menutup panggilan.")
    if not args.no_tts:
        speak("Denzyx siap, silakan bicara.", rate=args.rate, pitch=args.pitch,
              voice=args.voice_name, engine=args.engine)
    else:
        out("[Denz] Denzyx siap, silakan bicara.")

    silence = 0
    try:
        while True:
            out("\n[🎙 mendengar...]", end="\r" if not args.no_tts else "\n")
            text, e = listen(args.lang, timeout=args.listen_timeout)
            if e == "TIMEOUT":
                silence += 1
                if silence >= 3:
                    say("Saya tidak mendengar apa-apa. Bilang stop kalau mau "
                        "tutup, atau lanjut bicara.")
                    silence = 0
                continue
            if e == "PERMISSION":
                say("Tidak ada izin mikrofon. Aktifkan izin Termux dulu, "
                    "lalu coba lagi.")
                return 1
            if e == "EMPTY":
                continue
            if e:
                err(f"STT: {e}")
                return 1
            silence = 0
            text = text.strip()
            if not text:
                continue
            if _is_exit(text):
                say("Sampai jumpa!")
                break
            if args.wake:
                if args.wake.lower() not in text.lower():
                    say("Ya?")
                    continue
                text = re.sub(re.escape(args.wake), "", text, flags=re.I).strip()
            if not text:
                continue
            out("Kamu: " + text)
            reply, error = ask(state, text)
            if error:
                err(f"AI: {error}")
                say("Maaf, koneksi bermasalah. Coba lagi.")
                continue
            if not reply:
                say("Maaf, saya tidak dapat jawaban. Coba lagi.")
                continue
            state.messages.append({"role": "user", "content": text})
            state.messages.append({"role": "assistant", "content": reply})
            try:
                state.save_session()
            except Exception:  # noqa: BLE001
                pass
            say(reply)
            if args.listen_once:
                break
    except KeyboardInterrupt:
        out("\n[dibatalkan]")
        if not args.no_tts:
            speak("Sampai jumpa.", rate=args.rate, pitch=args.pitch,
                  voice=args.voice_name, engine=args.engine)
    out("\n=== 📞 Panggilan ditutup ===")
    return 0


if __name__ == "__main__":
    sys.exit(main())
