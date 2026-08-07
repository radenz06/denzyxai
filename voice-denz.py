#!/usr/bin/env python3
"""voice-denz.py — panggilan suara ke denzyx AI.

Dengar JERNIH: faster-whisper (STT neural offline, model int8) — rekam
via termux-microphone-record, transkrip bahasa Indonesia akurat. Kalau
faster-whisper nggak ada, fallback termux-speech-to-text.

Bicara NATURAL (suara cewe): Google Translate TTS bahasa Indonesia
(suara wanita Google yang natural) → kalau gagal edge-tts
id-ID-GadisNeural → kalau gagal TTS Android id-ID.

Percakapan disimpan ke sessions/ biar muncul di riwayat TUI. Reuse
mesin chat dari denzyx.py (retry, fallback key, persona
system_prompt.md).

Dependency tambahan (sangat disarankan):
    pip install faster-whisper        # STT jernih offline

Cara pakai:
    python3 voice-denz.py                 # mode call: terus dengar
    python3 voice-denz.py --listen-once   # dengar sekali, terus keluar
    python3 voice-denz.py --lang id-ID    # bahasa (default id-ID)
    python3 voice-denz.py --stt whisper   # paksa STT whisper
    python3 voice-denz.py --stt-model small   # model whisper lebih akurat
    python3 voice-denz.py --engine google # google | edge | android | auto
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
import urllib.parse
import urllib.request
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
DEFAULT_STT_MODEL = os.environ.get("DENZYX_STT_MODEL") or "base"

_UA = ("Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
       "(KHTML, like Gecko) Chrome/126.0 Safari/537.36")

# nudge anti-gema: minta AI nggak mirroring kata user
_VOICE_NUDGE = ("Mode panggilan suara: jawab langsung dan ringkas (2-4 "
                "kalimat), gaya ngobrol santai, JANGAN mengulang atau "
                "mencerminkan kata-kata user.")

_EMOJI_RX = re.compile(
    "[\U0001F000-\U0001FAFF\U00002600-\U000027BF\U0001F900-\U0001F9FF"
    "\U00002190-\U000021FF\U0000FE0F\U0000200D\U0001F1E6-\U0001F1FF]")
_SYMBOL_MAP = {"&": " dan ", "%": " persen ", "+": " plus ", "=": " sama dengan ",
               "#": " nomor "}


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


def _tts_text(text):
    """Siapin teks buat dibacain: buang markdown, emoji, ganti simbol."""
    text = _plain(text)
    text = _EMOJI_RX.sub(" ", text)
    for k, v in _SYMBOL_MAP.items():
        text = text.replace(k, v)
    text = re.sub(r"\s+", " ", text)
    return text.strip()


def _is_exit(text):
    low = text.strip().lower().strip(".!?")
    if low in EXIT_WORDS:
        return True
    return any(low.startswith(w) for w in ("stop", "matikan", "putus",
                                           "tutup", "sampai jumpa"))


# ---------------------------------------------------------------------------
# STT: whisper (jernih, offline) dengan fallback termux-speech-to-text
# ---------------------------------------------------------------------------

_STT_MODEL_CACHE = {}


def _stt_whisper_ok():
    try:
        import faster_whisper  # noqa: F401
        return True
    except Exception:  # noqa: BLE001
        return False


def _get_whisper(model_size):
    import faster_whisper
    if model_size not in _STT_MODEL_CACHE:
        _STT_MODEL_CACHE[model_size] = faster_whisper.WhisperModel(
            model_size, device="cpu", compute_type="int8")
    return _STT_MODEL_CACHE[model_size]


def _record_wav(path, seconds):
    exe = _which("termux-microphone-record")
    if not exe:
        return None, "termux-microphone-record tidak ada — pkg install termux-api"
    try:
        subprocess.run([exe, "-f", path, "-l", str(int(seconds))],
                       capture_output=True, text=True,
                       timeout=int(seconds) + 15)
    except subprocess.TimeoutExpired:
        pass
    except Exception as e:  # noqa: BLE001
        return None, str(e)
    if not os.path.exists(path) or os.path.getsize(path) < 200:
        return None, "rekaman kosong (mikrofon?)"
    return path, None


def _transcribe_whisper(wav, model_size, lang):
    try:
        model = _get_whisper(model_size)
        segs, _info = model.transcribe(wav, language=(lang or "id").split("-")[0])
        text = " ".join(s.text for s in segs).strip()
    except Exception as e:  # noqa: BLE001
        return None, str(e)
    if not text:
        return None, "EMPTY"
    return text, None


def _listen_termux(lang="id-ID", timeout=25):
    """STT Android (termux-speech-to-text) — fallback."""
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


def listen(lang="id-ID", timeout=25, stt="auto", record_seconds=6,
           stt_model=None):
    """Rekam 1 ucapan. Whisper (jernih) dulu, termux-speech-to-text
    kalau whisper nggak tersedia. Return (teks, err)."""
    stt = (stt or "auto").lower()
    if stt in ("auto", "whisper") and _stt_whisper_ok():
        wav = os.path.join(AUDIO_DIR, f"rec_{int(time.time() * 1000)}.wav")
        path, err = _record_wav(wav, record_seconds)
        if not err:
            try:
                text, err = _transcribe_whisper(wav, stt_model or DEFAULT_STT_MODEL,
                                                lang)
            finally:
                try:
                    os.unlink(wav)
                except OSError:
                    pass
            if text:
                return text, None
            if err:
                return None, err
        elif stt == "whisper":
            return None, err
    return _listen_termux(lang, timeout)


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


def _synth_google(text, out_path, lang="id"):
    """Google Translate TTS langsung via urllib (suara wanita id, natural).
    Tanpa dependency eksternal — nggak perlu package gtts."""
    tl = (lang or "id").split("-")[0]
    q = urllib.parse.quote(text)
    url = (f"https://translate.google.com/translate_tts?ie=UTF-8"
           f"&tl={tl}&client=tw-ob&q={q}")
    try:
        req = urllib.request.Request(url, headers={"User-Agent": _UA})
        with urllib.request.urlopen(req, timeout=30) as r:
            data = r.read()
    except Exception as e:  # noqa: BLE001
        return None, str(e)
    if not data:
        return None, "google tts mengembalikan kosong"
    with open(out_path, "wb") as fh:
        fh.write(data)
    return out_path, None


def _play_mp3(path):
    """Putar mp3 sampai habis (blocking) via termux-media-player.
    Durasi dihitung ffprobe, tidur sesuai durasi + jeda singkat — biar
    mic nggak nangkep suara sendiri waktu mau dengar lagi."""
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
        time.sleep(min(dur + 1.0, 90))
        subprocess.run([player, "stop"], capture_output=True, text=True,
                       timeout=10)
        time.sleep(0.3)
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
    """Buang mp3/wav lama (>1 hari) di folder audio biar nggak numpuk."""
    try:
        now = time.time()
        for pat in ("denz_*.mp3", "rec_*.wav"):
            for f in glob.glob(os.path.join(AUDIO_DIR, pat)):
                if now - os.path.getmtime(f) > 86400:
                    os.unlink(f)
    except Exception:  # noqa: BLE001
        pass


def speak(text, rate=1.0, pitch=1.0, voice=DEFAULT_VOICE, engine="auto",
          lang="id"):
    """Ucapkan teks — SUARA CEWE NATURAL:
    google (suara wanita Google id) → edge (GadisNeural) → android.
    Blocking."""
    text = _tts_text(text)
    if not text:
        return ""
    try:
        os.makedirs(AUDIO_DIR, exist_ok=True)
    except OSError:
        pass
    _tts_cleanup()
    engine = (engine or "auto").lower()
    chain = {"auto": ("google", "edge", "android"),
             "google": ("google",),
             "edge": ("edge",),
             "android": ("android",)}.get(engine, ("google",))
    last = ""
    for eng in chain:
        if eng == "google":
            path = os.path.join(AUDIO_DIR,
                                f"denz_{int(time.time() * 1000)}.mp3")
            path, err = _synth_google(text, path, lang)
        elif eng == "edge":
            path = os.path.join(AUDIO_DIR,
                                f"denz_{int(time.time() * 1000)}.mp3")
            path, err = _synth_mp3(text, path, voice, rate)
        else:
            r = _speak_android(text, rate, pitch)
            if "tidak tersedia" in r:
                last = r
                continue
            return r
        if path:
            perr = _play_mp3(path)
            try:
                os.unlink(path)
            except OSError:
                pass
            if not perr:
                return ""
            last = perr
            if "tidak ada" in perr:
                return perr
        else:
            last = err
    return f"TTS gagal: {last or 'tanpa error'}"


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
    ap.add_argument("--lang", default="id-ID",
                    help="bahasa STT + TTS (default id-ID)")
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
    ap.add_argument("--engine", choices=["auto", "google", "edge", "android"],
                    default="auto",
                    help="google = wanita natural (tanpa dep), edge = neural "
                         "cewe (butuh internet), android = TTS device, "
                         "auto = google lalu edge lalu android")
    ap.add_argument("--stt", choices=["auto", "whisper", "termux"],
                    default="auto",
                    help="whisper = STT neural offline (faster-whisper), "
                         "termux = termux-speech-to-text, auto = pakai "
                         "whisper kalau ada")
    ap.add_argument("--stt-model", default=DEFAULT_STT_MODEL,
                    help=f"model faster-whisper (default {DEFAULT_STT_MODEL}, "
                         "makin besar makin akurat tapi lambat)")
    ap.add_argument("--record-seconds", type=int, default=6,
                    help="durasi rekaman mic utk whisper (default 6)")
    args = ap.parse_args()

    state = denzyx.State()
    state.cwd = Path.cwd()

    def out(m="", end="\n"):
        print(m, end=end, flush=True)
    err = lambda m: print("!! " + m, file=sys.stderr, flush=True)

    def say(text):
        out("Denz: " + text)
        if not args.no_tts and text:
            msg = speak(_tts_text(text)[:4000], rate=args.rate,
                        pitch=args.pitch, voice=args.voice_name,
                        engine=args.engine, lang=args.lang)
            if msg and "tidak tersedia" in msg:
                err(msg)

    out("=== 📞 Panggilan suara — denzyx AI ===")
    if args.wake:
        out(f"Mode wake: sebut '{args.wake}' dulu biar direspons.")
    out(f"STT: {args.stt} · TTS: {args.engine} — bilang \"stop\" buat "
        "menutup panggilan.")
    if not args.no_tts:
        speak("Denzyx siap, silakan bicara.", rate=args.rate, pitch=args.pitch,
              voice=args.voice_name, engine=args.engine, lang=args.lang)
    else:
        out("[Denz] Denzyx siap, silakan bicara.")

    silence = 0
    nudge = {"role": "system", "content": _VOICE_NUDGE}
    try:
        while True:
            out("\n[🎙 mendengar...]", end="\r" if not args.no_tts else "\n")
            text, e = listen(args.lang, timeout=args.listen_timeout,
                             stt=args.stt, record_seconds=args.record_seconds,
                             stt_model=args.stt_model)
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
            state.messages.append(nudge)
            try:
                reply, error = ask(state, text)
            finally:
                state.messages.pop()
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
                  voice=args.voice_name, engine=args.engine, lang=args.lang)
    out("\n=== 📞 Panggilan ditutup ===")
    return 0


if __name__ == "__main__":
    sys.exit(main())
