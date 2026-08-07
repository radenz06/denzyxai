#!/usr/bin/env python3
"""voice-denz.py — panggilan suara ke denzyx AI.

Dengar pakai termux-speech-to-text (STT on-device Android, tanpa server),
ngomong balik pakai termux-tts-speak. Percakapan disimpan ke sessions/
biar muncul di riwayat TUI. Reuse mesin chat dari denzyx.py (retry,
fallback key, persona dari system_prompt.md).

Cara pakai:
    python3 voice-denz.py                 # mode call: terus dengar
    python3 voice-denz.py --listen-once   # dengar sekali, terus keluar
    python3 voice-denz.py --lang id-ID    # bahasa STT (default sistem)
    python3 voice-denz.py --no-tts        # tanpa suara, cuma teks
    python3 voice-denz.py --wake denz     # cuma respons kalau kata kunci
Bilang "stop" / "matikan" buat menutup panggilan.
"""

import argparse
import os
import queue
import re
import shutil
import subprocess
import sys
import threading
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import denzyx  # noqa: E402

EXIT_WORDS = ("stop", "matikan", "putus", "selesai", "keluar", "bye",
              "exit", "tutup", "sampai jumpa", "udahan", "sudah")


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


def speak(text, rate=1.0, pitch=1.0):
    """Ucapkan teks via termux-tts-speak (blocking biar mic nggak ketangkep)."""
    exe = _which("termux-tts-speak")
    if not exe:
        return "TTS tidak tersedia — pkg install termux-api"
    argv = [exe, "-r", str(rate), "-p", str(pitch), "-b"]
    try:
        proc = subprocess.run(argv, input=text, capture_output=True,
                              text=True, timeout=300)
        return (proc.stdout or "").strip() or (proc.stderr or "").strip()
    except subprocess.TimeoutExpired:
        return "TTS timeout"
    except Exception as e:  # noqa: BLE001
        return str(e)


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
                        pitch=args.pitch)
            if msg and "tidak tersedia" in msg:
                err(msg)

    out("=== 📞 Panggilan suara — denzyx AI ===")
    if args.wake:
        out(f"Mode wake: sebut '{args.wake}' dulu biar direspons.")
    out("Bilang \"stop\" buat menutup panggilan.")
    if not args.no_tts:
        speak("Denzyx siap, silakan bicara.", rate=args.rate, pitch=args.pitch)
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
            speak("Sampai jumpa.", rate=args.rate, pitch=args.pitch)
    out("\n=== 📞 Panggilan ditutup ===")
    return 0


if __name__ == "__main__":
    sys.exit(main())
