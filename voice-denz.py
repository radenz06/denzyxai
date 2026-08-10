#!/usr/bin/env python3
"""voice-denz.py — panggilan suara ke denzyx AI.

Dengar JERNIH: faster-whisper (STT neural offline, model int8) — rekam
via termux-microphone-record, transkrip bahasa Indonesia akurat. Kalau
faster-whisper nggak ada, fallback termux-speech-to-text.

Bicara NATURAL (suara cewe): Google Translate TTS bahasa Indonesia
(suara wanita Google yang natural) → kalau gagal edge-tts
id-ID-GadisNeural → kalau gagal TTS Android id-ID.

ADAPTIF (v2.6): ikut ritme bicara kamu (kecepatan TTS disesuaikan),
deteksi mood (ketawa/nangis/marah dari audio + kata), ganti suara pas
disuruh ("pakai suara cowok", "lebih cepat", "suara serak"), bisa
dengar sambil ngomong (barge-in — mulai bicara = TTS berhenti), dan
belajar ritme/nada kamu ke .denzyx/voice_profile.json.

Percakapan disimpan ke sessions/ biar muncul di riwayat TUI. Reuse
mesin chat dari denzyx.py (retry, fallback key, persona
system_prompt.md).

Dependency tambahan (sangat disarankan):
    pip install faster-whisper        # STT jernih offline
    pip install numpy                 # analisis ritme/nada/mood

Cara pakai:
    python3 voice-denz.py                 # mode call: terus dengar
    python3 voice-denz.py --listen-once   # dengar sekali, terus keluar
    python3 voice-denz.py --lang id-ID    # bahasa (default id-ID)
    python3 voice-denz.py --stt whisper   # paksa STT whisper
    python3 voice-denz.py --stt-model small   # model whisper lebih akurat
    python3 voice-denz.py --engine google # google | edge | android | auto
    python3 voice-denz.py --no-tts        # tanpa suara, cuma teks
    python3 voice-denz.py --no-barge-in   # matikan dengar sambil ngomong
    python3 voice-denz.py --no-learn      # matikan profil belajar
    python3 voice-denz.py --wake denz     # cuma respons kalau kata kunci
Bilang "stop" / "matikan" buat menutup panggilan.
"""

import argparse
import glob
import json
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
DENZYX_DIR = os.environ.get("DENZYX_HOME") or os.path.join(
    TERMUX_HOME, ".denzyx")
AUDIO_DIR = os.environ.get("DENZYX_TTS_DIR") or os.path.join(DENZYX_DIR, "tts")
PROFILE_PATH = os.path.join(DENZYX_DIR, "voice_profile.json")
DEFAULT_VOICE = os.environ.get("DENZYX_TTS_VOICE") or "id-ID-GadisNeural"
MALE_VOICE = "id-ID-ArdiNeural"
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


# ---------------------------------------------------------------------------
# Adaptive voice (v2.6): ritme, nada, emosi, ganti suara, barge-in, belajar
# ---------------------------------------------------------------------------

_EMO_CUE = {
    "tertawa": ("wkwk", "hahaha", "hehe", "hihi", "lucu", "😂", "😆", "🤣"),
    "sedih": ("sedih", "nangis", "huhu", "t_t", "kesepian", "sendiri",
              "🥺", "😢", "😭"),
    "marah": ("marah", "kesal", "bet", "geram", "benci", "goblok", "sial"),
    "ceria": ("hore", "senang", "seneng", "wow", "keren", "mantap",
              "yey", "asik", "semangat"),
}

# (rate_mult, pitch_mult, hint buat AI) — dipakai nyetel nada TTS & gaya jawab
_EMO_TTS = {
    "tertawa": (1.15, 1.08, "user lagi ketawa — jawab ringan, ikut ceria"),
    "ceria": (1.12, 1.05, "jawab semangat dan ceria"),
    "sedih": (0.85, 0.94, "jawab lembut, tenang, dan menghibur"),
    "marah": (1.05, 1.00, "jawab tenang dan tegas, jangan ikut marah"),
    "tegas": (1.00, 1.03, "jawab tegas dan jelas"),
    "netral": (1.00, 1.00, ""),
}


def _np():
    """numpy dipakai kalau ada; kalau nggak, fitur analisis audio dimatikan."""
    try:
        import numpy as _np_mod
        return _np_mod
    except Exception:  # noqa: BLE001
        return None


def _read_pcm(path, sr=16000):
    """Decode audio apa pun (mp3/wav/aac) ke mono float32 [-1,1] via ffmpeg."""
    if not _which("ffmpeg"):
        return None
    try:
        r = subprocess.run(
            ["ffmpeg", "-v", "error", "-i", path, "-f", "s16le", "-ac", "1",
             "-ar", str(sr), "-"], capture_output=True, timeout=90)
    except Exception:  # noqa: BLE001
        return None
    if r.returncode != 0 or not r.stdout:
        return None
    np = _np()
    data = np.frombuffer(r.stdout, dtype=np.int16)
    if not len(data):
        return None
    return data.astype(np.float32) / 32768.0, sr


def _auto_pitch(seg, sr):
    """F0 (Hz) per frame via autokorelasi, 0 kalau bukan suara (unvoiced)."""
    np = _np()
    seg = seg - seg.mean()
    energy = float(np.sum(seg ** 2))
    if energy < 1e-6:
        return 0.0
    corr = np.correlate(seg, seg, mode="full")[len(seg) - 1:]
    corr = corr / (corr[0] + 1e-9)
    lo, hi = max(1, int(sr / 400)), int(sr / 70)
    if hi - lo < 4 or hi >= len(corr):
        return 0.0
    peak = lo + int(np.argmax(corr[lo:hi]))
    if corr[peak] < 0.35:
        return 0.0
    return float(sr / peak)


def _analyze_audio(path, max_dur=20):
    """Ringkas karakter audio: durasi, RMS (dB), F0 rata2/std, rasio suara.
    Return dict atau None kalau numpy/ffmpeg nggak ada."""
    np = _np()
    if np is None:
        return None
    r = _read_pcm(path)
    if not r:
        return None
    x, sr = r
    dur = min(len(x) / sr, max_dur)
    frame = int(sr * 0.03)
    hop = int(sr * 0.02)
    rms_frames, f0_frames = [], []
    for i in range(0, min(len(x), int(max_dur * sr)) - frame, hop):
        seg = x[i:i + frame]
        rms_frames.append(float(np.sqrt(np.mean(seg ** 2) + 1e-9)))
        f0_frames.append(_auto_pitch(seg, sr))
    if not rms_frames:
        return None
    voice = [p for p in f0_frames if p > 0]
    rms_db = float(20 * np.log10(np.mean(rms_frames) + 1e-6))
    an = {"dur": round(dur, 2), "rms_db": round(rms_db, 1),
          "f0_mean": 0.0, "f0_std": 0.0,
          "voice_ratio": round(len(voice) / len(f0_frames), 2)}
    if voice:
        an["f0_mean"] = round(float(np.mean(voice)), 1)
        an["f0_std"] = round(float(np.std(voice)), 1)
    return an


def _emotion_from_text(text):
    t = (text or "").lower()
    score = {emo: sum(1 for c in cues if c in t)
             for emo, cues in _EMO_CUE.items()}
    best = max(score, key=score.get)
    return best if score[best] else "netral"


def _emotion_from_audio(an):
    """Heuristik dari energi + nada: ceria/sedih/tegas/netral."""
    if not an:
        return "netral"
    rdb, f, fs = an.get("rms_db", -60), an.get("f0_mean", 0), an.get("f0_std", 0)
    if rdb > -25 and f > 170 and fs > 40:
        return "ceria"
    if rdb < -38 and 0 < f < 130:
        return "sedih"
    if rdb > -28 and f > 150 and fs < 26:
        return "tegas"
    return "netral"


def _detect_voice_command(text):
    """Perintah user buat ganti suara/kecepatan/nada.
    Return dict: voice, rate, pitch, reset."""
    t = (text or "").lower()
    cmd = {}
    if re.search(r"\banak (kecil|kecil-kecil|kecil)\b|suara anak", t):
        cmd["voice"] = "child"
    elif re.search(r"\b(laki-laki|laki)\b|cowok|cowo|pria|\bmale\b|suara( )?laki", t):
        cmd["voice"] = "male"
    elif re.search(r"\b(cewek|cewe|perempuan|wanita)\b|\bfemale\b|suara cewe", t):
        cmd["voice"] = "female"
    if re.search(r"\b(lebih\s*)?(lambat|pelan|perlahan|plahan)\b", t):
        cmd["rate"] = cmd.get("rate", 1.0) * 0.88
    if re.search(r"\b(lebih\s*)?(cepat|kencang|gesit|cepet)\b", t):
        cmd["rate"] = cmd.get("rate", 1.0) * 1.12
    if re.search(r"suara (rendah|dalam|serak)\b", t):
        cmd["pitch"] = cmd.get("pitch", 1.0) * 0.88
    if re.search(r"suara (tinggi|melengking)\b", t):
        cmd["pitch"] = cmd.get("pitch", 1.0) * 1.15
    if re.search(r"suara normal|kembali normal|normal (lagi|aja)|balik normal", t):
        cmd["reset"] = True
    return cmd


def _speech_rate_est(text, an=None, recorded_seconds=None):
    """Kecepatan bicara user: karakter per detik."""
    if an and an.get("dur", 0) > 0:
        return len(text) / an["dur"]
    if recorded_seconds and recorded_seconds > 0:
        return len(text) / recorded_seconds
    return None


def _tts_rate_for_user(user_rate):
    """Petakan ritme user → rate TTS. ~12 kar/dtk itu natural (1.0)."""
    if not user_rate:
        return 1.0
    return min(max(user_rate / 12.0, 0.7), 1.35)


def _load_profile():
    try:
        with open(PROFILE_PATH) as fh:
            return json.load(fh)
    except Exception:  # noqa: BLE001
        return {}


def _save_profile(p):
    try:
        os.makedirs(os.path.dirname(PROFILE_PATH), exist_ok=True)
        with open(PROFILE_PATH, "w") as fh:
            json.dump(p, fh, indent=2)
    except Exception:  # noqa: BLE001
        pass


def _profile_update(key, val, alpha=0.35):
    """EMA — 'belajar' ritme & nada user lintas percakapan."""
    p = _load_profile()
    prev = p.get(key)
    p[key] = val if prev is None else round(prev * (1 - alpha) + val * alpha, 4)
    _save_profile(p)
    return p[key]


def _mic_rms(path):
    r = _read_pcm(path)
    if not r:
        return None
    x, _ = r
    np = _np()
    return float(np.sqrt(np.mean(x ** 2) + 1e-9))


def _play_mp3(path, barge_in=True):
    """Putar mp3 sampai habis. Kalau barge_in aktif, mic ikut merekam:
    user mulai ngomong (energi jauh di atas echo TTS) → playback langsung
    distop. Return (err, interrupted)."""
    player = _which("termux-media-player")
    if not player:
        return "termux-media-player tidak ada — pkg install termux-api", False
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
    except Exception as e:  # noqa: BLE001
        return str(e), False
    interrupted = False
    if barge_in and _which("termux-microphone-record") and _which("ffmpeg"):
        interrupted = _barge_in_watch(dur)
    if not interrupted:
        time.sleep(min(max(dur - 0.5, 0.1), 90))
        try:
            subprocess.run([player, "stop"], capture_output=True, text=True,
                           timeout=10)
        except Exception:  # noqa: BLE001
            pass
        time.sleep(0.3)
    return "", interrupted


def _barge_in_watch(dur):
    """Rekam chunk 0.8s berulang saat TTS main; user mulai ngomong →
    energi chunk jauh lebih besar dari echo TTS → stop. Return True."""
    mic = "termux-microphone-record"
    tmp = os.path.join(AUDIO_DIR, "barge")
    try:
        os.makedirs(tmp, exist_ok=True)
        probe = os.path.join(tmp, "p.wav")
        subprocess.run([mic, "-f", probe, "-l", "1"], capture_output=True,
                       text=True, timeout=10)
        echo = _mic_rms(probe) or 0.0
        floor = max(echo, 0.02)
        t_end = time.monotonic() + min(dur + 1.0, 90)
        cnt = 0
        while time.monotonic() < t_end:
            chunk = os.path.join(tmp, f"c{int(time.time() * 1000)}.wav")
            subprocess.run([mic, "-f", chunk, "-l", "1"], capture_output=True,
                           text=True, timeout=10)
            rms = _mic_rms(chunk)
            try:
                os.unlink(chunk)
            except OSError:
                pass
            if rms is None:
                break
            if rms > floor * 1.7 + 0.015:
                cnt += 1
                if cnt >= 2:
                    return True
            else:
                cnt = 0
        return False
    except Exception:  # noqa: BLE001
        return False
    finally:
        try:
            shutil.rmtree(tmp, ignore_errors=True)
        except Exception:  # noqa: BLE001
            pass


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
    kalau whisper nggak tersedia. Return (teks, err, analisis_audio)."""
    stt = (stt or "auto").lower()
    if stt in ("auto", "whisper") and _stt_whisper_ok():
        wav = os.path.join(AUDIO_DIR, f"rec_{int(time.time() * 1000)}.wav")
        path, err = _record_wav(wav, record_seconds)
        if not err:
            try:
                text, err = _transcribe_whisper(wav, stt_model or DEFAULT_STT_MODEL,
                                                lang)
                analysis = _analyze_audio(wav) if text else None
            finally:
                try:
                    os.unlink(wav)
                except OSError:
                    pass
            if text:
                return text, None, analysis
            if err:
                return None, err, None
        elif stt == "whisper":
            return None, err, None
    text, err = _listen_termux(lang, timeout)
    return text, err, None


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
          lang="id", barge_in=True):
    """Ucapkan teks — SUARA CEWE NATURAL:
    google (suara wanita Google id) → edge (GadisNeural) → android.
    Blocking; barge_in bikin playback berhenti kalau user mulai ngomong.
    Return (msg, interrupted)."""
    text = _tts_text(text)
    if not text:
        return "", False
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
            return r, False
        if path:
            perr, interrupted = _play_mp3(path, barge_in=barge_in)
            try:
                os.unlink(path)
            except OSError:
                pass
            if interrupted:
                return "", True
            if not perr:
                return "", False
            last = perr
            if "tidak ada" in perr:
                return perr, False
        else:
            last = err
    return f"TTS gagal: {last or 'tanpa error'}", False


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


def _clamp(x, lo, hi):
    return min(max(x, lo), hi)


def main():
    ap = argparse.ArgumentParser(prog="voice-denz")
    ap.add_argument("--lang", default="id-ID",
                    help="bahasa STT + TTS (default id-ID)")
    ap.add_argument("--rate", type=float, default=1.0,
                    help="kecepatan TTS awal (default 1.0)")
    ap.add_argument("--pitch", type=float, default=1.0,
                    help="nada TTS awal 0-2 (default 1.0)")
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
    ap.add_argument("--no-barge-in", action="store_true",
                    help="nonaktifkan dengar sambil ngomong (interupsi)")
    ap.add_argument("--no-learn", action="store_true",
                    help="nonaktifkan belajar ritme/nada (voice_profile.json)")
    args = ap.parse_args()

    state = denzyx.State()
    state.cwd = Path.cwd()

    def out(m="", end="\n"):
        print(m, end=end, flush=True)
    err = lambda m: print("!! " + m, file=sys.stderr, flush=True)

    # profil belajar (ritme & nada) — dipakai antar percakapan
    prof = _load_profile()
    cfg = {"voice": prof.get("pref_voice") or args.voice_name,
           "engine": prof.get("pref_engine") or args.engine,
           "rate": _clamp(args.rate, 0.6, 1.6),
           "pitch": _clamp(args.pitch, 0.6, 1.8)}
    if not args.no_learn and prof.get("rate_bias"):
        cfg["rate"] = _clamp(prof["rate_bias"], 0.6, 1.6)

    def say(text, emo_hint=""):
        out("Denz: " + text)
        if not args.no_tts and text:
            msg, interrupted = speak(
                _tts_text(text)[:4000], rate=cfg["rate"], pitch=cfg["pitch"],
                voice=cfg["voice"], engine=cfg["engine"], lang=args.lang,
                barge_in=not args.no_barge_in)
            if msg and "tidak tersedia" in msg:
                err(msg)
            return interrupted
        return False

    out("=== 📞 Panggilan suara — denzyx AI ===")
    if args.wake:
        out(f"Mode wake: sebut '{args.wake}' dulu biar direspons.")
    out(f"STT: {args.stt} · TTS: {cfg['engine']} · suara: {cfg['voice']} — "
        "bilang \"stop\" buat menutup panggilan.")
    out("Coba: \"pakai suara cowok\", \"lebih cepat\", \"suara serak\", atau "
        "ketawa biar aku ikut mood.")
    if not args.no_tts:
        speak("Denzyx siap, silakan bicara. Aku bakal belajar ritme bicaramu.",
              rate=cfg["rate"], pitch=cfg["pitch"], voice=cfg["voice"],
              engine=cfg["engine"], lang=args.lang, barge_in=False)
    else:
        out("[Denz] Denzyx siap, silakan bicara.")

    silence = 0
    try:
        while True:
            out("\n[🎙 mendengar...]", end="\r" if not args.no_tts else "\n")
            text, e, analysis = listen(args.lang, timeout=args.listen_timeout,
                                       stt=args.stt,
                                       record_seconds=args.record_seconds,
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

            # ---- adaptive: emosi, ritme, ganti suara, belajar ----
            cmd = _detect_voice_command(text)
            mood = _emotion_from_text(text)
            if mood == "netral":
                mood = _emotion_from_audio(analysis)
            emo_rate_mult, emo_pitch_mult, emo_hint = _EMO_TTS.get(
                mood, _EMO_TTS["netral"])
            user_rate = _speech_rate_est(text, analysis,
                                         args.record_seconds)
            rhythm = _tts_rate_for_user(user_rate)
            if not args.no_learn:
                if user_rate:
                    _profile_update("chars_per_sec", user_rate)
                if analysis and analysis.get("f0_mean"):
                    _profile_update("f0_hz", analysis["f0_mean"])
            if cmd.get("reset"):
                cfg.update(voice=args.voice_name, engine=args.engine,
                           rate=_clamp(args.rate, 0.6, 1.6),
                           pitch=_clamp(args.pitch, 0.6, 1.8))
            if cmd.get("voice"):
                if cmd["voice"] == "male":
                    cfg["voice"], cfg["engine"] = MALE_VOICE, "edge"
                    ack = "Oke, suaraku jadi cowok."
                elif cmd["voice"] == "female":
                    cfg["voice"], cfg["engine"] = DEFAULT_VOICE, args.engine
                    ack = "Oke, suaraku jadi cewek."
                else:
                    cfg["voice"], cfg["engine"] = args.voice_name, "android"
                    cfg["pitch"] = 1.45
                    ack = "Oke, sekarang aku bicara kayak anak kecil."
                if not args.no_learn:
                    _profile_update("pref_voice", cfg["voice"])
                    _profile_update("pref_engine", cfg["engine"])
            else:
                ack = ""
            if cmd.get("rate"):
                cfg["rate"] = _clamp(cfg["rate"] * cmd["rate"], 0.6, 1.6)
            if cmd.get("pitch"):
                cfg["pitch"] = _clamp(cfg["pitch"] * cmd["pitch"], 0.6, 1.8)
            if not args.no_learn:
                _profile_update("rate_bias", cfg["rate"])
            eff_rate = _clamp(rhythm * cfg["rate"] * emo_rate_mult, 0.6, 1.6)
            eff_pitch = _clamp(cfg["pitch"] * emo_pitch_mult, 0.6, 1.8)
            info = (f"[mood: {mood} · ritme {user_rate:.1f} kar/dtk → "
                    f"rate {eff_rate:.2f}]" if user_rate
                    else f"[mood: {mood} → rate {eff_rate:.2f}]")
            out(info if not args.no_tts else info + " (bisu)")
            if ack:
                out(ack)

            out("Kamu: " + text)
            hint = emo_hint + (f" Baru saja user minta suara ganti."
                               if ack else "")
            nudge = {"role": "system",
                     "content": (_VOICE_NUDGE + (" " + hint if hint else ""))}
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
            speak("Sampai jumpa.", rate=cfg["rate"], pitch=cfg["pitch"],
                  voice=cfg["voice"], engine=cfg["engine"], lang=args.lang,
                  barge_in=False)
    out("\n=== 📞 Panggilan ditutup ===")
    return 0


if __name__ == "__main__":
    try:
        import lic
        lic.require()
    except SystemExit:
        raise
    except Exception:  # noqa: BLE001
        pass
    sys.exit(main())
