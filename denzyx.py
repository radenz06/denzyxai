#!/usr/bin/env python3
"""
denzyx AI — AI agent coding TUI (menu interaktif, bukan CLI).

Tanpa API key → free tier otomatis (cost 0). Key dibaca otomatis
dari file auth standard bila tersedia, di-refresh tiap request.

Navigasi:
  ↑/↓ / j/k  : pindah menu
  Enter      : pilih
  ESC        : kembali / batal
  Ctrl-C     : keluar

Sesi tersimpan otomatis di ~/.denzyx_sessions/*.json
"""

import curses
import fcntl
import json
import os
import queue
import select
import signal
import struct
import sys
import termios
import time
import datetime
from pathlib import Path


# System prompt dimuat dari file .md di samping app — edit bebas,
# tidak ada lagi fitur ubah system prompt di dalam app.
SYSTEM_PROMPT_FILE = Path(__file__).resolve().parent / "system_prompt.md"
THEME_FILE = Path(__file__).resolve().parent / "theme.md"


def load_system_prompt():
    """Baca system prompt dari file .md. File kosong/tidak ada = kosong."""
    try:
        if SYSTEM_PROMPT_FILE.exists():
            txt = SYSTEM_PROMPT_FILE.read_text(encoding="utf-8").strip()
            if txt:
                return txt
    except OSError:
        pass
    return ""


# Default tema DENZYX: biru/cyan gelap + hitam AMOLED. Tiap pasang warna
# bisa di-override dari file theme.md di samping app (format: nama = fg,bg;
# nilai angka 0-255, -1 = warna default terminal).
DEFAULT_THEME = {
    "banner":      (51, -1),        # 1  highlight/banner/judul/footer (cyan)
    "user":        (curses.COLOR_GREEN, -1),   # 2  user
    "assistant":   (curses.COLOR_WHITE, -1),   # 3  assistant
    "yellow":      (curses.COLOR_YELLOW, -1),  # 4  model list/reasoning
    "error":       (196, -1),       # 5  error
    "label_user":  (curses.COLOR_WHITE, 24),   # 6  label KAMU
    "label_ai":    (curses.COLOR_WHITE, 27),   # 7  label AI DENZYX
    "label_tool":  (curses.COLOR_WHITE, 30),   # 8  label TOOL
    "label_error": (curses.COLOR_WHITE, 196),  # 9  label ERROR
    "md_heading":  (39, -1),       # 10 heading markdown
    "md_code":     (curses.COLOR_YELLOW, -1),  # 11 kode markdown
    "md_bullet":   (curses.COLOR_GREEN, -1),   # 12 bullet markdown
    "md_quote":    (39, -1),      # 13 kutipan markdown
}


def load_theme():
    """Baca tema dari file theme.md (nama = fg,bg per baris).
    Tidak ada file / baris rusak / nilai aneh = pakai default."""
    theme = dict(DEFAULT_THEME)
    try:
        if not THEME_FILE.exists():
            return theme
        for raw in THEME_FILE.read_text(encoding="utf-8").splitlines():
            line = raw.strip()
            if not line or line.startswith("#"):
                continue
            if "=" not in line:
                continue
            name, _, val = line.partition("=")
            name, val = name.strip(), val.strip()
            if name not in theme:
                continue
            try:
                fg, _, bg = val.partition(",")
                fg, bg = int(fg.strip()), int(bg.strip())
            except ValueError:
                continue
            if -1 <= fg <= 255 and -1 <= bg <= 255:
                theme[name] = (fg, bg)
    except OSError:
        pass
    return theme


def _wib_time():
    """Waktu Indonesia Barat (UTC+7, tanpa DST). Fallback offset manual
    bila zoneinfo/tzdata tidak tersedia."""
    try:
        from zoneinfo import ZoneInfo
        return datetime.datetime.now(ZoneInfo("Asia/Jakarta"))
    except Exception:  # noqa: BLE001
        return (datetime.datetime.now(datetime.timezone.utc)
                + datetime.timedelta(hours=7))
import threading
import time
import unicodedata
import urllib.request
import urllib.error
import re
import random
import shutil
import subprocess
import http.client
import socket

ZEN_URL = "https://opencode.ai/zen/v1/chat/completions"
DIRECT_URL = "https://api.deepseek.com/v1/chat/completions"
SESSION_DIR = Path(__file__).resolve().parent / "sessions"
CRASH_LOG = Path(os.path.expanduser("~/.denzyx_crash.log"))

APP_NAME = "denzyx AI"
APP_VERSION = "2.6.0"

_CLI_HELP = """\
denzyx AI v{ver} — AI agent buat Termux

Cara pakai:
  ./denzyx                buka menu utama (TUI)
  ./denzyx --voice        panggilan suara (dengar & bicara ke AI)
  ./denzyx --help         bantuan ini

Opsi tambahan yang diteruskan ke mode voice (lihat voice-denz.py):
  --voice --lang id-ID --stt whisper --stt-model base --engine auto
         --rate 1.1 --wake denz --no-tts --no-barge-in --no-learn
""".format(ver=APP_VERSION)
AUTH_PATHS = [
    Path(os.path.expanduser("~/.local/share/opencode/auth.json")),
    Path(os.path.expanduser("~/.config/opencode/auth.json")),
    Path(os.path.expanduser("~/.opencode/auth.json")),
]

USER_AGENT = ("Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
              "(KHTML, like Gecko) Chrome/126.0 Safari/537.36")

# tool machinery dibagi dengan dscli (bash, read, write, edit, glob, grep)
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import dscli as _dscli  # noqa: E402

TOOLS = _dscli.TOOLS
TOOL_IMPL = _dscli.TOOL_IMPL
SAFE_TOOLS = _dscli.SAFE_TOOLS

FORE, BACK = 0, 0  # diisi di init


def find_opencode_key():
    """Baca API key dari auth.json standard — paham semua format:
    lama {"<provider>": {"apiKey": ...}}, baru {"<provider>": {"type":
    "api", "key": ...}}, oauth {"type": "oauth", "tokens": {access_token}}.
    Prioritas provider: utama, zen, deepseek, lalu provider apa pun.
    Dipanggil fresh tiap request → auto-update tanpa restart app."""

    def pick(entry):
        if isinstance(entry, str) and entry:
            return str(entry)
        if isinstance(entry, dict):
            for f in ("apiKey", "key"):
                v = entry.get(f)
                if isinstance(v, str) and v:
                    return v
            tok = entry.get("tokens")
            if isinstance(tok, dict):
                for f in ("access_token", "accessToken"):
                    v = tok.get(f)
                    if isinstance(v, str) and v:
                        return v
        return None

    for path in AUTH_PATHS:
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
            if isinstance(data, dict):
                for prov in ("opencode", "opencodeZen", "zen",
                             "deepseek", "deepseek-api", "anthropic"):
                    v = pick(data.get(prov))
                    if v:
                        return v
                for v in data.values():
                    v2 = pick(v)
                    if v2:
                        return v2
        except (OSError, json.JSONDecodeError):
            continue
    return None


def resolve_key(direct, api_key=None):
    if api_key:
        return api_key
    env = os.environ.get("OPENCODE_API_KEY") or os.environ.get("DEEPSEEK_API_KEY")
    if env:
        return env
    oc = find_opencode_key()
    if oc:
        return oc
    if not direct:
        return "public"
    return None


class State:
    def __init__(self):
        self.direct = False
        self.model = "deepseek-v4-flash-free"
        self.api_key = None
        self.temperature = 1.0
        self.max_tokens = 8192
        self.show_reasoning = True
        self.agent_mode = True     # tool calling aktif
        self.auto_allow = True     # eksekusi tool tanpa konfirmasi
        self.cwd = Path.cwd()      # folder kerja AI (semua tool relatif ke sini)
        self.messages = []          # riwayat chat aktif
        self.session_title = ""
        self.saved_id = None
        self._last_key = None
        self._key_changed = False
        self._used_public = False  # key ditolak -> fallback FREE

    @property
    def url(self):
        return DIRECT_URL if self.direct else ZEN_URL

    @property
    def system(self):
        # dibaca FRESH tiap request -> edit system_prompt.md langsung aktif,
        # tanpa restart, berlaku untuk sesi lama maupun baru
        return load_system_prompt()

    @property
    def key(self):
        # dibaca FRESH tiap request -> key terbaru langsung kepakai
        # langsung kepakai tanpa restart app
        k = resolve_key(self.direct, self.api_key)
        if k != self._last_key:
            self._last_key = k
            self._key_changed = True
        return k

    def save_session(self):
        SESSION_DIR.mkdir(parents=True, exist_ok=True)
        if not self.messages:
            # semua pesan terhapus → buang file sesi yang lama
            if self.saved_id and Path(self.saved_id).exists():
                try:
                    Path(self.saved_id).unlink()
                except OSError:
                    pass
                self.saved_id = None
            return None
        # normalisasi: content None (pesan tool_calls tanpa teks) -> ""
        clean = []
        for m in self.messages:
            m = dict(m)
            if m.get("content") is None:
                m["content"] = ""
            clean.append(m)
        first = clean[0].get("content") if clean else ""
        title = self.session_title or (first[:40] if first else "untitled")
        # kalau sudah pernah disimpan, update file yang sama (satu sesi = satu file)
        if self.saved_id and Path(self.saved_id).exists():
            fname = Path(self.saved_id)
        else:
            fname = SESSION_DIR / f"{_wib_time():%Y%m%d-%H%M%S}.json"
            self.saved_id = str(fname)
        data = {
            "title": title,
            "model": self.model,
            "direct": self.direct,
            "temperature": self.temperature,
            "max_tokens": self.max_tokens,
            "agent_mode": self.agent_mode,
            "auto_allow": self.auto_allow,
            "messages": clean,
        }
        fname.write_text(json.dumps(data, ensure_ascii=False, indent=2),
                         encoding="utf-8")
        return fname

    def load_session(self, path):
        data = json.loads(Path(path).read_text(encoding="utf-8"))
        # model tetap satu (deepseek via Zen) — abaikan model dari file lama
        # system prompt SELALU dari file system_prompt.md — sesi lama
        # tidak menimpa (hapus baris system dari sesi)
        self.temperature = data.get("temperature", 1.0)
        self.max_tokens = data.get("max_tokens", 8192)
        self.agent_mode = data.get("agent_mode", True)
        self.auto_allow = data.get("auto_allow", True)
        self.messages = [
            {**m, "content": m.get("content") or ""}
            for m in data.get("messages", [])
        ]
        self.session_title = data.get("title", "")
        self.saved_id = path


_TRANSIENT_TERMS = (
    "closed connection", "remote closed", "connection reset",
    "connection aborted", "reset by peer", "broken pipe", "refused",
    "name or service", "timed out", "timeout", "eof", "bad status line",
    "temporarily", "server disconnected", "end of stream",
)


def _transient_err(exc):
    """Apakah error layak retry otomatis (koneksi putus / 5xx / 429)?
    Endpoint FREE sering menutup koneksi sebelum kirim byte apa pun."""
    if isinstance(exc, urllib.error.HTTPError):
        return exc.code in (408, 429, 500, 502, 503, 504)
    if isinstance(exc, urllib.error.URLError):
        r = exc.reason
        if isinstance(r, (ConnectionResetError, ConnectionAbortedError,
                          BrokenPipeError, http.client.RemoteDisconnected,
                          socket.timeout)):
            return True
        s = str(r).lower()
        return any(k in s for k in _TRANSIENT_TERMS)
    if isinstance(exc, (ConnectionResetError, ConnectionAbortedError,
                        BrokenPipeError, http.client.RemoteDisconnected,
                        socket.timeout)):
        return True
    s = str(exc).lower()
    return any(k in s for k in _TRANSIENT_TERMS)


def _api_stream(state, msgs, tools, out_queue, stop_evt=None, timeout=180,
                visible=True):
    """1 request stream (dgn auto-retry bila koneksi putus sebelum delta).
    Return (content, reasoning, calls). Dipakai stream_chat, stream_agent,
    dan sub-agent (tool task). visible=False → delta tidak di-stream ke
    out_queue (sub-agent, hasilnya lewat 'note')."""
    payload = {
        "model": state.model,
        "messages": msgs,
        "temperature": state.temperature,
        "max_tokens": state.max_tokens,
        "stream": True,
    }
    if tools is not None:
        payload["tools"] = tools
        payload["tool_choice"] = "auto"

    def open_req(key):
        req = urllib.request.Request(
            state.url,
            data=json.dumps(payload).encode("utf-8"),
            headers={
                "Content-Type": "application/json",
                "Authorization": f"Bearer {key}",
                "User-Agent": USER_AGENT,
            },
            method="POST",
        )
        return urllib.request.urlopen(req, timeout=timeout)

    seen_retry = False
    last_exc = None
    for attempt in range(4):
        if stop_evt is not None and stop_evt.is_set():
            return "", "", []
        had_delta = [False]
        content_parts = []
        reasoning_parts = []
        tool_acc = {}      # index -> {"idx","id","name","args"}
        try:
            try:
                resp = open_req(state.key)
            except urllib.error.HTTPError as e:
                # key ditolak -> auto fallback ke public (FREE)
                if e.code in (401, 403) and not state.direct:
                    state._used_public = True
                    resp = open_req("public")
                else:
                    raise
            with resp:
                for raw in resp:
                    if stop_evt is not None and stop_evt.is_set():
                        return "", "", []
                    line = raw.decode("utf-8", errors="replace").strip()
                    if not line.startswith("data:"):
                        continue
                    data = line[5:].strip()
                    if data == "[DONE]":
                        break
                    try:
                        chunk = json.loads(data)
                    except json.JSONDecodeError:
                        continue
                    delta = (chunk.get("choices") or [{}])[0].get("delta", {})
                    piece = delta.get("content")
                    if piece:
                        had_delta[0] = True
                        content_parts.append(piece)
                        if visible:
                            out_queue.put(("content", piece))
                    rpiece = delta.get("reasoning_content")
                    if rpiece:
                        had_delta[0] = True
                        reasoning_parts.append(rpiece)
                        if visible:
                            out_queue.put(("reasoning", rpiece))
                    for tc in delta.get("tool_calls") or []:
                        had_delta[0] = True
                        idx = tc.get("index", 0)
                        slot = tool_acc.setdefault(idx, {
                            "idx": idx, "id": None,
                            "name": "", "args": ""})
                        if tc.get("id"):
                            slot["id"] = tc["id"]
                        fn = tc.get("function") or {}
                        if fn.get("name"):
                            slot["name"] = fn["name"]
                        if fn.get("arguments"):
                            slot["args"] += fn["arguments"]
            calls = [{"id": s["id"], "name": s["name"],
                      "arguments": s["args"]}
                     for s in sorted(tool_acc.values(),
                                     key=lambda v: v["idx"])]
            if not content_parts and reasoning_parts:
                # ENDPOINT FREE kadang kirim jawaban HANYA di
                # reasoning_content (content kosong) → tanpa fallback ini
                # blok AI DENZYX tampil KOSONG = user mengira AI tak menjawab.
                # Pakai reasoning sebagai isi jawaban (tetap ditampilkan
                # juga sebagai PIKIR saat show_reasoning on).
                out_queue.put(("note", "↻ jawaban diambil dari reasoning AI"))
                content_parts, reasoning_parts = reasoning_parts, []
                if visible:
                    for piece in content_parts:
                        out_queue.put(("content", piece))
            if seen_retry:
                out_queue.put(("note",
                               "↻ koneksi sempat putus — lanjut otomatis ✓"))
            return "".join(content_parts), "".join(reasoning_parts), calls
        except urllib.error.HTTPError as e:
            if had_delta[0] or not _transient_err(e):
                raise
            try:
                e.read()
            except Exception:  # noqa: BLE001
                pass
        except urllib.error.URLError as e:
            if had_delta[0] or not _transient_err(e):
                raise
            last_exc = e
        except Exception as e:  # noqa: BLE001
            if had_delta[0] or not _transient_err(e):
                raise
            last_exc = e
        # koneksi putus SEBELUM delta → jeda, coba lagi
        seen_retry = True
        time.sleep(min(6.0, 1.0 * (2 ** attempt)))
    if last_exc is not None:
        raise last_exc
    raise urllib.error.URLError(
        "koneksi terputus berkali-kali (retry habis)")


def stream_chat(state, prompt, out_queue, stop_evt=None):
    """Thread worker: stream ke out_queue sebagai list of (kind, text)."""
    messages = []
    if state.system:
        messages.append({"role": "system", "content": state.system})
    messages.append({"role": "system",
                     "content": f"Folder kerja aktif: {state.cwd}"})
    messages.extend(state.messages)
    messages.append({"role": "user", "content": prompt})
    messages = _compact_context(state, messages)

    try:
        content, reasoning, calls = _api_stream(
            state, messages, None, out_queue, stop_evt)
    except urllib.error.HTTPError as e:
        body = e.read().decode("utf-8", errors="replace")[:500]
        out_queue.put(("error", f"HTTP {e.code}: {body}"))
        out_queue.put(("done", None))
        return
    except urllib.error.URLError as e:
        out_queue.put(("error", str(e.reason)))
        out_queue.put(("done", None))
        return
    except Exception as e:  # noqa: BLE001
        out_queue.put(("error", str(e)))
        out_queue.put(("done", None))
        return
    if stop_evt is not None and stop_evt.is_set():
        return
    if not content and not reasoning and not calls:
        # respons kosong TOTAL → jangan diam-diam "selesai"
        out_queue.put(("error", "respons kosong dari AI — coba kirim ulang"))
    out_queue.put(("done", None))


def _norm_calls(calls):
    """Normalisasi format tool_calls: OpenAI nested -> internal
    {"id","name","arguments"} (dipakai resume dari sesi tersimpan)."""
    out = []
    for c in calls or []:
        fn = c.get("function") or {}
        out.append({"id": c.get("id"),
                    "name": c.get("name") or fn.get("name"),
                    "arguments": (c.get("arguments")
                                  or fn.get("arguments") or "{}")})
    return out


def _resolve_tool_args(name, args, cwd):
    """Resolve path relatif tool ke folder kerja aktif (state.cwd)."""
    def ap(p):
        p = os.path.expanduser(str(p))
        return p if os.path.isabs(p) else str(Path(cwd) / p)

    if name == "bash":
        if not args.get("workdir"):
            args["workdir"] = str(cwd)
        else:
            args["workdir"] = ap(args["workdir"])
    elif name in ("read", "write", "edit"):
        if args.get("path"):
            args["path"] = ap(args["path"])
    elif name in ("glob", "grep"):
        if args.get("path"):
            args["path"] = ap(args["path"])
        else:
            args["path"] = str(cwd)
    return args


def _prune_orphan_tools(msgs, keep_tail=False):
    """Buang pesan assistant ber-tool_calls yang tool response-nya TIDAK
    lengkap (tertinggal saat stream di-stop/ESC/crash). API Console
    menolak 400 kalau ada tool_calls tanpa tool response yang mengikutinya.
    keep_tail=True: pertahankan pesan TERAKHIR yang tool-nya belum dijawab
    — itu milik jalur resume yang akan mengeksekusi ulang tool-nya."""
    out = []
    i = 0
    n = len(msgs)
    while i < n:
        m = msgs[i]
        if m.get("role") == "assistant" and m.get("tool_calls"):
            ids = [c.get("id") for c in m["tool_calls"]]
            rows = []
            j = i + 1
            while j < n and msgs[j].get("role") == "tool":
                rows.append(msgs[j])
                j += 1
            got = {r.get("tool_call_id") for r in rows}
            missing = [c for c in ids if c not in got]
            if missing:
                # ada tool call yang tak berjawab
                if keep_tail and j >= n:
                    out.append(m)
                    out.extend(rows)
                # else: buang assistant + tool rows yang mengikutinya
                i = j
                continue
            out.append(m)
            out.extend(rows)
            i = j
            continue
        out.append(m)
        i += 1
    return out


SUBAGENT_MAX_DEPTH = 4
SUBAGENT_MAX_TURNS = 40

CONTEXT_CHAR_LIMIT = 300_000      # total char konteks → dipicu kompaksi
COMPACT_KEEP_LAST = 6            # pesan terakhir yg dipertahankan penuh
COMPACT_SUMMARY_TEXT = 110_000   # teks lama yang disertakan ke ringkasan


def _summarize(state, text):
    """Rangkum blok percakapan (non-stream, request terpisah). Return
    ringkasan atau None bila gagal."""
    payload = {
        "model": state.model,
        "messages": [
            {"role": "system",
             "content": "Kamu perangkum. Rangkum percakapan di bawah jadi "
                        "ringkas (bahasa Indonesia) tanpa menghilangkan "
                        "permintaan yang belum selesai, nama/path file, dan "
                        "keputusan penting. Balas teks ringkasan saja."},
            {"role": "user", "content": text},
        ],
        "temperature": 0.2,
        "max_tokens": 900,
    }

    def one(key):
        req = urllib.request.Request(
            state.url, data=json.dumps(payload).encode("utf-8"),
            headers={"Content-Type": "application/json",
                     "Authorization": f"Bearer {key}",
                     "User-Agent": USER_AGENT},
            method="POST")
        with urllib.request.urlopen(req, timeout=60) as r:
            return json.loads(r.read().decode("utf-8", errors="replace"))

    for _attempt in range(3):
        try:
            try:
                data = one(state.key)
            except urllib.error.HTTPError as e:
                if e.code in (401, 403) and not state.direct:
                    state._used_public = True
                    data = one("public")
                else:
                    raise
            msg = (data.get("choices") or [{}])[0].get("message", {})
            out = msg.get("content") or ""
            if not out.strip():
                out = msg.get("reasoning_content") or ""
            if out.strip():
                return out.strip()
        except Exception:  # noqa: BLE001
            pass
        time.sleep(0.8)
    return None


def _compact_context(state, messages):
    """Kompresi konteks ala opencode: kalau total isi terlalu besar,
    pesan lama dirangkum model → system ringkasan + ekor pesan terbaru.
    Tak mengubah state.messages (hanya salinan yang dikirim ke API)."""
    total = sum(
        len(m.get("content") or "") + len(m.get("reasoning_content") or "")
        for m in messages)
    if total <= CONTEXT_CHAR_LIMIT:
        return messages
    tail = list(messages[-COMPACT_KEEP_LAST:])
    head = messages[:-COMPACT_KEEP_LAST]
    blob_parts = []
    for m in head:
        role = m.get("role", "")
        text = str(m.get("content") or "")
        if not text.strip():
            continue
        blob_parts.append(f"[{role}] {text}")
    blob = "\n".join(blob_parts)
    if len(blob) > COMPACT_SUMMARY_TEXT:
        blob = blob[-COMPACT_SUMMARY_TEXT:]
    summary = _summarize(state, blob) if blob.strip() else None
    if summary:
        out = [{"role": "system",
                "content": "Ringkasan percakapan sebelumnya (dikompres):\n"
                           + summary}] + _prune_orphan_tools(tail)
    else:
        # fallback: tetap buang bagian lama biar konteks terpangkas
        out = _prune_orphan_tools(tail)
    return out


def _run_subagent(state, prompt, out_queue, stop_evt=None, depth=1):
    """Delegasi ke sub-agent otonom (ala tool task di opencode).
    Konteks BARU: system prompt + instruksi user. Semua tool sub-agent
    jalan TANPA konfirmasi (auto-allow). Aktivitasnya disiarkan sebagai
    baris 'note'; hasil akhir (max 6000) dikembalikan sbg output tool."""
    if depth > SUBAGENT_MAX_DEPTH:
        return "error: kedalaman delegasi maks 4"
    msgs = []
    if state.system:
        msgs.append({"role": "system", "content": state.system})
    msgs.append({"role": "system",
                 "content": f"Folder kerja aktif: {state.cwd}"})
    msgs.append({"role": "user", "content": prompt or "(tanpa instruksi)"})
    out_queue.put(("note", f"↳ sub-agent #{depth}: {prompt[:110]}"))
    parts = []
    for _turn in range(SUBAGENT_MAX_TURNS):
        if stop_evt is not None and stop_evt.is_set():
            return "✋ dihentikan user (ESC)"
        content, reasoning, calls = _api_stream(
            state, msgs, TOOLS, out_queue, stop_evt, timeout=300,
            visible=False)
        if content:
            parts.append(content)
        elif reasoning:
            parts.append(reasoning)
        if not calls:
            return "\n\n".join(parts).strip() or "(sub-agent selesai tanpa teks)"
        for c in calls:
            if stop_evt is not None and stop_evt.is_set():
                return "✋ dihentikan user (ESC)"
            name, arg_s = c["name"], c["arguments"]
            try:
                args = json.loads(arg_s or "{}")
                if not isinstance(args, dict):
                    args = {}
            except json.JSONDecodeError:
                args = {"raw": arg_s}
            if name == "task":
                out_queue.put(("note", "↳ sub-agent ⤵ sub-agent (delegasi)"))
                result = _run_subagent(
                    state, str(args.get("prompt") or ""),
                    out_queue, stop_evt, depth + 1)
            else:
                fn = TOOL_IMPL.get(name)
                try:
                    if fn:
                        args = _resolve_tool_args(name, args, state.cwd)
                        result = fn(**args)
                    else:
                        result = f"error: tool tidak dikenal: {name}"
                except TypeError as e:
                    result = f"error: argumen tidak valid: {e}"
                except Exception as e:  # noqa: BLE001
                    result = f"error: {e}"
                summary = result.replace("\n", " ")[:140]
                out_queue.put(("note", f"↳ sub-agent: {name} → {summary}"))
            if len(result) > 6000:
                result = result[:6000] + "\n[output terpotong]"
            msgs.append({
                "role": "assistant",
                "content": content or None,
                "reasoning_content": reasoning or None,
                "tool_calls": [
                    {"id": c["id"], "type": "function",
                     "function": {"name": c["name"],
                                  "arguments": c["arguments"]}}],
            })
            msgs.append({"role": "tool", "tool_call_id": c["id"],
                         "content": result})
    return "✋ sub-agent melewati batas langkah (40)"


def stream_agent(state, prompt, out_queue, decision_queue, stop_evt=None,
                 resume=False):
    """Thread worker AGENT: loop stream + tool calling + konfirmasi user.
    out_queue: ("content"/"reasoning"/"tool_pending"/"tool_result"/"done"/"error")
    decision_queue: user jawab "y"/"n"/"a" utk tiap tool call.
    resume=True + prompt=None: eksekusi tool call terakhir yang kepotong
    (stop streaming) lalu lanjut, tanpa menambah pesan user baru."""
    messages = []
    if state.system:
        messages.append({"role": "system", "content": state.system})
    messages.append({"role": "system",
                     "content": f"Folder kerja aktif: {state.cwd} — "
                                "path relatif tool di-resolve ke folder ini"})
    messages.extend(state.messages)
    messages = _prune_orphan_tools(messages, keep_tail=resume)
    if resume and (state.messages and state.messages[-1].get("role")
                   == "assistant" and state.messages[-1].get("tool_calls")):
        prompt = None
    if prompt is not None:
        # JANGAN append prompt di sini: pemanggil (send_prompt / /continue)
        # SUDAH menambahkannya ke state.messages. Kalau ditambah lagi → API
        # menerima pesan user dobel/triple (model bilang "kamu ngirim 3x").
        # Cek jaga-jaga: kalau prompt ternyata BELUM ada di state, baru
        # tambahkan (mis. dipanggil langsung dari kode lain).
        if not (state.messages
                and state.messages[-1].get("role") == "user"
                and state.messages[-1].get("content") == prompt
                or (len(state.messages) >= 2
                    and state.messages[-1].get("role") == "assistant"
                    and not state.messages[-1].get("content")
                    and state.messages[-2].get("role") == "user"
                    and state.messages[-2].get("content") == prompt)):
            messages.append({"role": "user", "content": prompt})
    if not resume:
        messages = _compact_context(state, messages)

    def handle_calls(calls):
        """Eksekusi tool calls (konfirmasi kecuali auto-allow) lalu lanjut.
        Return False bila user minta stop."""
        if stop_evt is not None and stop_evt.is_set():
            return False
        out_queue.put(("tool_pending", calls))
        # tunggu keputusan user (y / n / a=always) — KECUALI auto-allow
        # aktif: langsung eksekusi tanpa menunggu konfirmasi
        verdict = "n"
        if state.auto_allow:
            verdict = "y"
        else:
            try:
                verdict = decision_queue.get(timeout=600)
            except queue.Empty:
                pass
        if stop_evt is not None and stop_evt.is_set():
            return False
        if verdict == "a":
            state.auto_allow = True
        for c in calls:
            name = c["name"]
            try:
                args = json.loads(c["arguments"] or "{}")
                if not isinstance(args, dict):
                    args = {}
            except json.JSONDecodeError:
                args = {"raw": c["arguments"]}
            if state.auto_allow or verdict in ("y", "a"):
                if name == "task":
                    # delegasi ke sub-agent otonom (ala opencode Task)
                    try:
                        result = _run_subagent(
                            state, str(args.get("prompt") or ""),
                            out_queue, stop_evt)
                    except Exception as e:  # noqa: BLE001
                        result = f"error: {e}"
                else:
                    fn = TOOL_IMPL.get(name)
                    try:
                        if fn:
                            args = _resolve_tool_args(name, args, state.cwd)
                            result = fn(**args)
                        else:
                            result = f"error: tool tidak dikenal: {name}"
                    except TypeError as e:
                        result = f"error: argumen tidak valid: {e}"
                    except Exception as e:  # noqa: BLE001
                        result = f"error: {e}"
            else:
                result = ("user menolak menjalankan tool ini. "
                          "Beri tahu user dan tanyakan langkah lain.")
            if len(result) > 6000:
                result = result[:6000] + "\n[output terpotong]"
            tool_msg = {"role": "tool", "tool_call_id": c["id"],
                        "content": result}
            messages.append(tool_msg)
            state.messages.append(tool_msg)
            out_queue.put(("tool_result", (name, args, result)))
        return True

    try:
        if prompt is None:
            # resume: eksekusi tool call terakhir yang kepotong dulu
            if not handle_calls(_norm_calls(state.messages[-1]["tool_calls"])):
                return
        while True:
            if stop_evt is not None and stop_evt.is_set():
                return
            content, reasoning, calls = _api_stream(
                state, messages, TOOLS, out_queue, stop_evt, timeout=300)
            if not calls:
                # selesai tanpa tool: konten sudah di-stream per-delta ke
                # state.messages oleh main thread (handler "content").
                # JANGAN tulis ulang di sini — race dgn main thread yang
                # masih memproses delta sisa dari queue → ekor duplikat
                # di file sesi ("...kerjakan! kerjakan!").
                if not content and not reasoning:
                    # respons kosong TOTAL → jangan diam-diam "selesai"
                    # (user mengira AI tidak menjawab)
                    out_queue.put(("error",
                                   "respons kosong dari AI — coba kirim ulang"))
                out_queue.put(("done", None))
                return
            if stop_evt is not None and stop_evt.is_set():
                return
            # bersihkan stub assistant kosong dari chat_screen
            if (state.messages and state.messages[-1]["role"] == "assistant"
                    and not state.messages[-1].get("content")
                    and not state.messages[-1].get("tool_calls")):
                state.messages.pop()
            # ada tool call → simpan pesan assistant dgn tool_calls
            assistant_msg = {
                "role": "assistant",
                "content": content or None,
                "reasoning_content": reasoning or None,
                "tool_calls": [
                    {"id": c["id"], "type": "function",
                     "function": {"name": c["name"],
                                  "arguments": c["arguments"]}}
                    for c in calls
                ],
            }
            messages.append(assistant_msg)
            state.messages.append(assistant_msg)
            if not handle_calls(calls):
                return
    except urllib.error.HTTPError as e:
        body = e.read().decode("utf-8", errors="replace")[:500]
        out_queue.put(("error", f"HTTP {e.code}: {body}"))
        out_queue.put(("done", None))
    except urllib.error.URLError as e:
        out_queue.put(("error", str(e.reason)))
        out_queue.put(("done", None))
    except Exception as e:  # noqa: BLE001
        out_queue.put(("error", str(e)))
        out_queue.put(("done", None))


# ---------------------------------------------------------------------------
# UI helpers
# ---------------------------------------------------------------------------

def draw_frame(win, title, status=""):
    h, w = win.getmaxyx()
    try:
        win.border(0)
        if title:
            t = clip_width(f" {title} ", max(1, w - 2))
            x = max(1, (w - len(t)) // 2)
            win.addstr(0, x, t, curses.A_BOLD | curses.color_pair(1))
        if status:
            s = status[: w - 3]
            # h-2, bukan h-1: baris h-1 dipakai footer menu (menu_list)
            win.addstr(h - 2, 2, s, curses.A_DIM)
    except curses.error:
        pass
    # PENTING: TIDAK noutrefresh di sini! Kalau direfresh duluan, sel
    # frame tidak ditandai ulang oleh refresh akhir pemanggil → clear
    # stdscr pertama (mis. menu utama) menghapus frame dari virtual
    # screen → border/judul/status hilang di render pertama.


def wrap_text(text, width):
    if width <= 1:
        return [text]
    words, lines, cur = text.split(), [], ""
    for w in words:
        if len(cur) + len(w) + 1 <= width:
            cur = f"{cur} {w}".strip()
        else:
            if cur:
                lines.append(cur)
            cur = w
            while len(cur) > width:
                lines.append(cur[:width])
                cur = cur[width:]
    if cur:
        lines.append(cur)
    return lines or [""]


def clip_width(text, max_cols):
    """Potong teks berdasarkan lebar tampilan (aman utk karakter lebar/emoji)."""
    cols, out = 0, []
    for ch in text:
        w = 2 if unicodedata.east_asian_width(ch) in ("W", "F") else 1
        if cols + w > max_cols:
            break
        cols += w
        out.append(ch)
    return "".join(out)


def md_blocks(text):
    """Tag tiap baris pesan: md_code (fence), md_heading, md_quote,
    md_bullet, atau plain — dengan state blok kode ```/~~~."""
    out, infence = [], False
    for ln in text.split("\n"):
        s = ln.lstrip()
        if infence:
            if s.startswith(("```", "~~~")):
                infence = False
            out.append(("md_code", ln))
            continue
        if s.startswith(("```", "~~~")):
            infence = True
            out.append(("md_code", ln))
        elif s.startswith("#"):
            out.append(("md_heading", ln))
        elif s.startswith((">", "»")):
            out.append(("md_quote", ln))
        elif s.startswith(("- ", "* ", "+ ")) or re.match(r"^\d{1,3}\.\s", s):
            out.append(("md_bullet", ln))
        else:
            out.append(("plain", ln))
    return out


_MD_INLINE = re.compile(
    r"(`[^`\n]+`|\*\*[^*\n]+\*\*|__[^_\n]+__|\[[^\]\n]+\]\([^)\n]*\))")


def md_inline_segs(text, base_attr):
    """Pecah satu baris jadi segmen ber-attr utk inline `kode`, **tebal**,
    __tebal__, dan [link](url). Tanpa token inline = None (render biasa)."""
    parts = _MD_INLINE.split(text)
    if len(parts) == 1:
        return None
    segs = []
    for i, p in enumerate(parts):
        if not p:
            continue
        if i % 2 == 1:
            if p.startswith("`"):
                segs.append((p[1:-1], curses.color_pair(11)))
            elif p.startswith(("**", "__")):
                segs.append((p[2:-2], base_attr | curses.A_BOLD))
            else:
                m = p.find("](")
                segs.append((p[1:m], base_attr | curses.A_BOLD))
        else:
            segs.append((p, base_attr))
    return segs


def notify_done():
    """Notifikasi + vibrate Termux saat AI selesai. Diam bila termux-api
    tidak terpasang / dimatikan via DENZYX_NOTIFY=0."""
    if os.environ.get("DENZYX_NOTIFY") == "0":
        return
    try:
        if shutil.which("termux-notification"):
            subprocess.Popen(
                ["termux-notification", "--id", "denzyx",
                 "--title", "denzyx AI",
                 "--content", "AI selesai bekerja — buka app untuk lihat"],
                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        if shutil.which("termux-vibrate"):
            subprocess.Popen(["termux-vibrate", "-d", "150"],
                             stdout=subprocess.DEVNULL,
                             stderr=subprocess.DEVNULL)
    except Exception:  # noqa: BLE001
        pass


# ---------------------------------------------------------------------------
# Input system: baca /dev/tty LANGSUNG di thread + antrian.
# NCURSES getch TIDAK dipakai (parser-nya random makan \x1b[201~ / hang
# dengan write besar) — ncurses hanya untuk OUTPUT.
# ---------------------------------------------------------------------------

_PASTE_START = 2000   # \x1b[200~ (awal bracketed paste)
_PASTE_END = 2001     # \x1b[201~ (akhir bracketed paste)
_KEY_ALT_ENTER = 2002     # \x1b\r — baris baru lembut (tidak kirim pesan)
_KEY_SHIFT_ENTER = 2003   # \x1b[13;2u / \x1b[27;2;13~ — baris baru lembut
_KEY_CLICK = 2004         # klik mouse SGR (\x1b[<b;x;yM) — posisi di _click_pos
_click_pos = (0, 0)       # koordinat klik terakhir (x, y), 1-based layar

_input_q = queue.Queue()
_input_stop = threading.Event()
_input_buf = b""  # sisa bytes yang belum diproses


_input_reader_th = None  # thread pembaca input (watchdog di _gch)


def _input_reader():
    try:
        fd = os.open("/dev/tty", os.O_RDWR)
    except OSError:
        return
    try:
        while not _input_stop.is_set():
            try:
                r, _, _ = select.select([fd], [], [], 0.2)
                if not r:
                    continue
                data = os.read(fd, 4096)
                if not data:
                    break
                _input_q.put(data)
            except OSError:
                break
            except Exception:  # noqa: BLE001
                # JANGAN mati diam-diam (EINTR dll) — coba lagi
                time.sleep(0.01)
    finally:
        try:
            os.close(fd)
        except OSError:
            pass


def _ensure_reader():
    """Watchdog: kalau thread reader mati, nyalakan lagi — tanpa ini app
    jadi buta terhadap semua input (klik & keyboard) diam-diam."""
    global _input_reader_th
    th = _input_reader_th
    if th is None or not th.is_alive():
        _input_reader_th = threading.Thread(target=_input_reader, daemon=True)
        _input_reader_th.start()


def _gch(timeout=-1):
    """Ambil 1 byte input mentah. timeout=-1 = blocking; timeout>=0 =
    kembalikan None kalau habis. NCURSES TIDAK menyentuh byte ini —
    \x1b SELALU muncul utuh."""
    global _input_buf
    deadline = None if timeout == -1 else time.monotonic() + timeout
    while True:
        if _input_buf:
            ch = _input_buf[0]
            _input_buf = _input_buf[1:]
            return ch
        try:
            if deadline is None:
                data = _input_q.get(timeout=0.05)
            else:
                rem = deadline - time.monotonic()
                if rem <= 0:
                    return None
                data = _input_q.get(timeout=min(0.05, rem))
        except queue.Empty:
            # JANGAN return di sini! Satu slice kosong ≠ timeout habis —
            # lanjut loop, cek rem <= 0 di atas. Tanpa ini, _gch(0.15)
            # cuma menunggu 50ms (satu slice), bukan 150ms → ekor
            # sequence SGR yang telat bocor ke input sebagai teks.
            _ensure_reader()  # watchdog: reader mati? nyalakan lagi
            continue
        _input_buf = data


def _read_seq():
    """Baca kelanjutan ESC sequence dari antrian (timeout 40ms).
    Return string sequence (mis. "[200~", "[A") atau "" kalau ESC asli."""
    parts = []
    while True:
        # SGR mouse ([<...) sering terpotong antar-chunk di HP (sentuhan/
        # drag) — kasih waktu ekstra biar ekor sequence keburu nyusul.
        # Sequence normal (panah, paste, dll) tetap cepat (terminate di
        # byte penanda), jadi timeout panjang tidak menambah lag ketikan.
        so_far = "".join(parts)
        c = _gch(0.15 if so_far.startswith("[<") else 0.04)
        if c is None:
            break
        parts.append(chr(c))
        joined = "".join(parts)
        if c == 126 or c in (ord("t"), ord("u"), ord("Z")):
            break
        if len(parts) >= 2 and parts[0] == "[" and c in (ord("A"), ord("B"),
                                                          ord("C"), ord("D"),
                                                          ord("H"), ord("F")):
            break
        if len(parts) >= 2 and parts[0] == "O" and c in (ord("P"), ord("Q"),
                                                         ord("R"), ord("S"),
                                                         ord("H"), ord("F")):
            break
        if len(parts) >= 2 and parts[0] == "[" and parts[1] == "<" and c in (ord("M"), ord("m")):
            break  # SGR mouse: [< bnum ; x ; y M/m — terminasi di M/m
        if len(parts) > 16:
            break
    return "".join(parts)


def _drain_pending(win=0.08):
    """Buang byte sisa yang masih mengantre (ekor burst mouse/junk).
    Dipanggil setelah sequence tidak dikenal — mencegah sisa byte
    bocor sebagai teks ke input."""
    global _input_buf
    end = time.monotonic() + win
    while time.monotonic() < end:
        _input_buf = b""
        try:
            while not _input_q.empty():
                _input_q.get_nowait()
        except queue.Empty:
            pass
        if _gch(0.02) is None:
            break  # sepi 20ms → selesai
    _input_buf = b""
    try:
        while not _input_q.empty():
            _input_q.get_nowait()
    except queue.Empty:
        pass


_last_resize_t = 0.0  # waktu KEY_RESIZE terakhir (untuk debounce stray key)
_resize_pending = False  # SIGWINCH diterima, belum dikonsumsi jadi KEY_RESIZE


def _on_sigwinch(signum, frame):
    """SIGWINCH (zoom/unzoom HP, rotasi, resize terminal) — Termux TIDAK
    selalu mengirim laporan \x1b[8;H;Wt, jadi debounce stray-key butuh
    sinyal ini. Handler lama (ncurses) tetap dipanggil supaya ukuran
    layar internal curses ikut diperbarui."""
    global _resize_pending, _last_resize_t
    _resize_pending = True
    _last_resize_t = time.monotonic()
    old = getattr(_on_sigwinch, "_old", None)
    if old is not None:
        try:
            old(signum, frame)
        except Exception:  # noqa: BLE001
            pass


def _sync_term_size():
    """Pastikan ukuran internal ncurses (LINES/COLS) mengikuti terminal.
    Dipanggil saat SIGWINCH dikonsumsi — untuk kasus handler lama
    (ncurses) tidak bisa dirantai, agar render memakai ukuran baru."""
    try:
        fd = os.open("/dev/tty", os.O_RDONLY)
        try:
            sz = fcntl.ioctl(fd, termios.TIOCGWINSZ, b"\0" * 8)
            rows, cols = struct.unpack("HHHH", sz)[:2]
            if rows and cols:
                curses.resizeterm(rows, cols)
        finally:
            os.close(fd)
    except Exception:  # noqa: BLE001
        pass


def _next_key(timeout=-1):
    """Wrapper _next_key_inner + proteksi HP/terminal mobile:
    - KEY_RESIZE dicatat waktunya.
    - SIGWINCH: key pertama setelah sinyal dikonsumsi sebagai KEY_RESIZE
      (bukan diteruskan!) — Termux/Android sering mengirim '\r' nyasar
      saat keyboard hide/show saat zoom/rotasi → dulu bisa membuka sesi
      baru. Setelah itu, key aksi dalam 400ms DIABAIKAN."""
    global _last_resize_t, _resize_pending
    ch = _next_key_inner(timeout)
    if _resize_pending:
        # key pertama setelah resize dijadikan notifikasi resize saja
        _resize_pending = False
        _last_resize_t = time.monotonic()
        _sync_term_size()
        return curses.KEY_RESIZE
    if ch == curses.KEY_RESIZE:
        _last_resize_t = time.monotonic()
        return ch
    if ch in (10, 13, 27, curses.KEY_UP, curses.KEY_DOWN,
              curses.KEY_LEFT, curses.KEY_RIGHT, curses.KEY_PPAGE,
              curses.KEY_NPAGE, curses.KEY_HOME, curses.KEY_END,
              curses.KEY_BTAB):
        if time.monotonic() - _last_resize_t < 0.4:
            return None  # stray key dari resize: buang
    return ch


def _next_key_inner(timeout=-1):
    """Baca 1 key (int, seperti getch) ATAU None (timeout habis).
    ESC sequences diparsing manual → curses.KEY_* / _PASTE_START/_PASTE_END.
    Sequence tidak dikenal dibuang (None)."""
    ch = _gch(timeout)
    if ch is None:
        return None
    if ch != 27:
        return ch
    seq = _read_seq()
    if seq == "[200~":
        return _PASTE_START
    if seq == "[201~":
        return _PASTE_END
    if seq == "":
        return 27  # ESC asli
    if seq in ("[A", "[B", "[C", "[D", "[H", "[F",
               "OH", "OF", "[5~", "[6~", "[3~"):
        return {"[A": curses.KEY_UP, "[B": curses.KEY_DOWN,
                "[C": curses.KEY_RIGHT, "[D": curses.KEY_LEFT,
                "[H": curses.KEY_HOME, "[F": curses.KEY_END,
                "OH": curses.KEY_HOME, "OF": curses.KEY_END,
                "[5~": curses.KEY_PPAGE, "[6~": curses.KEY_NPAGE,
                "[3~": curses.KEY_DC}[seq]
    if seq.startswith("[8;"):
        return curses.KEY_RESIZE  # \x1b[8;H;Wt
    if seq == "[Z":
        return curses.KEY_BTAB  # shift+tab
    if seq == "\r":
        return _KEY_ALT_ENTER  # alt+enter
    if seq in ("[13;2u", "[27;2;13~", "[13;5u", "[13;13~"):
        return _KEY_SHIFT_ENTER  # shift+enter / ctrl+enter (kitty/xterm)
    fkey = {"OP": curses.KEY_F1, "OQ": curses.KEY_F2, "OR": curses.KEY_F3,
            "OS": curses.KEY_F4, "[15~": curses.KEY_F5, "[17~": curses.KEY_F6,
            "[18~": curses.KEY_F7, "[19~": curses.KEY_F8, "[20~": curses.KEY_F9,
            "[21~": curses.KEY_F10, "[23~": curses.KEY_F11,
            "[24~": curses.KEY_F12}.get(seq)
    if fkey is not None:
        return fkey
    if seq.startswith("[<"):
        # SGR mouse: wheel up=64 (scroll atas), down=65 (scroll bawah);
        # klik kiri/tengah/kanan (bnum 0/1/2/3, press "M") → _KEY_CLICK.
        # Drag (32/35) & release ("m") diabaikan.
        try:
            bnum = int(seq[2:].split(";")[0])
            if bnum == 64:
                return curses.KEY_PPAGE
            if bnum == 65:
                return curses.KEY_NPAGE
            if bnum in (0, 1, 2, 3) and seq.endswith("M"):
                p = seq[2:].split(";")
                global _click_pos
                _click_pos = (int(p[1]), int(p[2].rstrip("Mm")))
                return _KEY_CLICK
        except Exception:  # noqa: BLE001
            pass
        if seq[-1:] not in ("M", "m"):
            _drain_pending()  # sequence terpotong → sisa byte bisa bocor
        return None  # drag/release/unknown: abaikan
    _drain_pending()  # sisa byte dari sequence rusak → buang, jangan bocor
    return None  # sequence tidak dikenal: buang


class _MatrixRain:
    """Hujan digital ala cmatrix untuk latar menu & sidebar sesi.

    Tiap kolom = 1 berkas karakter hijau yang jatuh: sel terdepan (kepala)
    terang & tebal, sisanya meredup ke atas. Karakter dipakai karakter
    lebar-tunggal (ASCII) supaya tidak merusak perhitungan kolom curses."""
    _CHARS = ("0123456789ABCDEFGHIJKLMNOPQRSTUVWXYZ"
              "abcdefghijklmnopqrstuvwxyz!@#$%^&*()_+-=[]{}<>?/|;:.,")

    def __init__(self, h, w):
        self.h = self.w = 0
        self.cols = []
        self.resize(h, w)

    def resize(self, h, w):
        h, w = max(1, h), max(1, w)
        if h == self.h and w == self.w:
            return
        self.h, self.w = h, w
        self.cols = []
        for _ in range(self.w):
            self.cols.append([
                random.randrange(-self.h * 3, 0),      # posisi kepala (y)
                random.randrange(max(2, self.h // 5),
                                 max(3, self.h)),       # panjang berkas
            ])

    def step(self):
        for c in self.cols:
            c[0] += 1
            if c[0] >= self.h + c[1]:
                c[0] = random.randrange(-self.h * 3, -1)
                c[1] = random.randrange(max(2, self.h // 5),
                                        max(3, self.h))

    def draw(self, win):
        try:
            for x, (head, length) in enumerate(self.cols):
                for k in range(length):
                    y = head - k
                    if 0 <= y < self.h:
                        ch = random.choice(self._CHARS)
                        if k == 0:
                            attr = curses.A_BOLD | curses.color_pair(2)
                        elif k == 1:
                            attr = curses.color_pair(2)
                        else:
                            attr = curses.A_DIM | curses.color_pair(2)
                        try:
                            win.addch(y, x, ch, attr)
                        except curses.error:
                            pass
        except curses.error:
            pass


def menu_list(stdscr, title, items, selected, status="", footer="",
              banner=None, status_fn=None, matrix=False):
    """Render menu; return (key, choice). key=10 utk Enter, 27 ESC.
    choice = indeks item yang sedang dipilih — untuk klik langsung
    (SGR mouse dari layar HP), item dihitung dari baris layar & dikunci
    sebagai choice, lalu dikembalikan bersama key 10 (Enter).
    banner: daftar baris ASCII art (opsional, dipakai menu utama).
    status_fn: callable → string status live (dipanggil tiap render ulang,
    misal jam real-time)."""
    h, w = stdscr.getmaxyx()
    win = curses.newwin(h, w, 0, 0)
    back = None                 # window blank layar penuh (di-set di _render)
    item_top = 2  # baris layar (0-based) item pertama — utk pemetaan klik
    bx = by = box_w = box_h = 0
    rain = _MatrixRain(h, w) if (matrix and w >= 24) else None

    def _render():
        nonlocal item_top, bx, by, box_w, box_h, back
        st = status_fn() if status_fn else status
        # --- ukuran kotak (konten) ---
        tw = len(title) + 2
        iw = max((len(l) + len(d) + 6) for l, d in items) if items else 0
        bw = max(len(ln) for ln in banner) if banner else 0
        fw = len(footer) + 8
        box_w = min(w - 2, max(tw, iw, bw, fw) + 4)
        rows = 2
        if banner and h >= 12:
            rows += len(banner) + 1
        rows += len(items) + 1
        box_h = min(h - 2, rows + 2)
        bx = max(0, (w - box_w) // 2)
        by = max(0, (h - box_h) // 2)
        # Blank SELURUH layar pakai window terpisah seukuran layar. JANGAN
        # pakai win.clear() sebelum resize — ncurses resize() di python
        # MEMBATALKAN flag clear, jadi menu/menu lama di belakang tidak
        # kebersih dan numpuk (terverifikasi di tes terisolasi).
        back = curses.newwin(h, w, 0, 0)
        back.erase()
        if rain is not None:
            rain.resize(h, w)
            rain.draw(back)
        try:
            win.resize(box_h, box_w)
            win.mvwin(by, bx)
        except curses.error:
            bx, by = 0, 0
        win.erase()   # bersihkan area kotak (residue dari render lama)
        win.border(0)
        # judul di tengah kotak
        t = clip_width(f" {title} ", max(1, box_w - 2))
        tx = max(1, (box_w - len(t)) // 2)
        try:
            win.addstr(0, tx, t, curses.A_BOLD | curses.color_pair(1))
        except curses.error:
            pass
        y = 2
        # ASCII art banner — di-center sebagai SATU blok (offset sama utk
        # semua baris). Kalau di-center per baris, offset beda-beda →
        # art kelihatan miring/rusak.
        if banner and h >= 12:
            aw = max(len(ln) for ln in banner)
            ax = max(1, (box_w - aw) // 2)
            for bl in banner:
                try:
                    win.addnstr(y, ax, bl, box_w - ax - 1,
                                curses.A_BOLD | curses.color_pair(1))
                except curses.error:
                    pass
                y += 1
            y += 1  # baris kosong pemisah banner & item
        item_top = by + y
        for i, (label, desc) in enumerate(items):
            if y >= box_h - 2:
                break
            # potong deskripsi kalau nggak muat — JANGAN biarkan nabrak label
            lw = len(label) + 4
            dl = len(desc)
            avail = max(6, box_w - lw - 2)
            if dl > avail:
                desc = desc[:max(0, avail - 1)] + "…"
                dl = len(desc)
            if i == selected:
                try:
                    win.addstr(y, 2, "► ", curses.A_BOLD | curses.color_pair(1))
                    win.addnstr(y, 4, label, box_w - 6,
                                curses.A_BOLD | curses.color_pair(1))
                except curses.error:
                    pass
            else:
                try:
                    win.addstr(y, 2, "  ")
                    win.addnstr(y, 4, label, box_w - 6)
                except curses.error:
                    pass
            if desc:
                try:
                    win.addnstr(y, box_w - dl - 2, desc, dl, curses.A_DIM)
                except curses.error:
                    pass
            y += 1
        if footer:
            try:
                # watermark DENZYX konsisten di semua footer menu
                ftxt = f"{footer} · DENZYX"
                win.addstr(box_h - 1, 2, ftxt[: box_w - 4],
                           curses.A_BOLD | curses.color_pair(1))
            except curses.error:
                pass
        # PENTING: stdscr di-refresh DULU, win TERAKHIR (z-order window di atas)
        stdscr.noutrefresh()
        back.noutrefresh()
        win.noutrefresh()
        curses.doupdate()

    _render()
    result = None
    # matrix mode: idle pendek (~15fps) biar hujan beranimasi hidup
    idle_wait = 0.06 if rain is not None else 0.5
    while True:
        ch = _next_key(idle_wait)
        if ch is None:      # idle timeout → render ulang (jam hidup + rain)
            if rain is not None:
                rain.step()
            _render()
            continue
        if ch in (_PASTE_START, _PASTE_END):
            continue
        if ch == _KEY_CLICK:
            # klik item menu (baris layar item_top+1 dst) → pilih + Enter.
            # _click_pos 1-based, item_top 0-based → kurangi 1.
            _cx, cy = _click_pos
            if not (bx <= _cx < bx + box_w):
                continue  # klik di luar kotak: abaikan
            item = cy - item_top - 1
            if 0 <= item < len(items) and item_top + 1 + item < h - 2:
                result = (10, item)
                break
            continue  # klik di luar item: abaikan
        result = (ch, selected)
        break
    # Bersihkan layar sebelum kembali ke pemanggil: window kotak (win) dan
    # window blank (back) adalah window TERPISAH yang z-ordernya di atas —
    # kalau tidak di-erase + di-refresh, sisa kotak menu MENUMPUK di layar
    # setelah menu ditutup (terutama di chat: kotak menutupi area output).
    if back is not None:
        win.erase()
        back.erase()
        stdscr.noutrefresh()
        back.noutrefresh()
        win.noutrefresh()
        curses.doupdate()
    return result


def input_line(stdscr, prompt, initial="", maxlen=200):
    """Input satu baris; return string atau None (ESC)."""
    h, w = stdscr.getmaxyx()
    win = curses.newwin(3, w, h // 2 - 1, 0)
    buf = list(initial)
    while True:
        win.erase()
        try:
            win.border(0)
            win.addstr(0, 1, prompt[: w - 2], curses.A_BOLD)
            shown = "".join(buf)[-(w - 4):]
            win.addstr(1, 1, shown)
        except curses.error:
            pass
        win.noutrefresh()
        # PENTING: stdscr dulu, win terakhir (z-order di atas)
        stdscr.noutrefresh()
        curses.doupdate()
        while True:
            ch = _next_key()
            if ch in (_PASTE_START, _PASTE_END):
                continue
            break
        if ch in (27,):
            return None
        if ch in (10, 13):
            return "".join(buf)
        if ch in (curses.KEY_BACKSPACE, 127, 8):
            if buf:
                buf.pop()
        elif 32 <= ch <= 126 and len(buf) < maxlen:
            buf.append(chr(ch))
        elif ch == curses.KEY_RESIZE:
            return "RESIZE"


def confirm(stdscr, message):
    h, w = stdscr.getmaxyx()
    # bersihkan layar sekali (hindari numpuk di atas menu/dialog lain)
    _blank_screen(h, w)
    win = curses.newwin(5, min(60, w - 4), h // 2 - 2, max(0, (w - 60) // 2))
    while True:
        win.erase()
        try:
            win.border(0)
            for i, line in enumerate(wrap_text(message, 52)):
                if i < 3:
                    win.addstr(i + 1, 2, line)
            win.addstr(4, 2, "[y] ya   [n] tidak", curses.A_DIM)
        except curses.error:
            pass
        win.noutrefresh()
        # PENTING: stdscr dulu, win terakhir (z-order di atas)
        stdscr.noutrefresh()
        curses.doupdate()
        while True:
            ch = _next_key()
            if ch in (_PASTE_START, _PASTE_END):
                continue
            break
        if ch in (ord("y"), ord("Y")):
            return True
        if ch in (ord("n"), ord("N"), 27):
            return False
        if ch == curses.KEY_RESIZE:
            return False

# ---------------------------------------------------------------------------
# TUI-style: helpers + chat screen (sidebar sesi + output + input + status)
# ---------------------------------------------------------------------------

# ---------------------------------------------------------------------------
# ---------------------------------------------------------------------------

MODELS = ["deepseek-v4-flash-free"]
# label tampilan model (ID asli tetap dikirim ke API)
MODEL_LABEL = {"deepseek-v4-flash-free": "denzyx AI"}


def _model_label(m):
    return MODEL_LABEL.get(m, m)

# Banner ASCII "DENZYX" (figlet). small: layar ~62 kolom (HP potret);
# standard: layar lebar. Dipilih otomatis dari lebar terminal di
# menu utama. Baris kosong terakhir dibuang; trailing space dirapikan.
BANNERS = {
"small": [
        ' ___  ___ _  _ ______   ____  __',
        '|   \\| __| \\| |_  /\\ \\ / /\\ \\/ /',
        '| |) | _|| .` |/ /  \\ V /  >  <',
        '|___/|___|_|\\_/___|  |_|  /_/\\_\\',
    ],
    "standard": [
        ' ____  _____ _   _ _______   ____  __',
        '|  _ \\| ____| \\ | |__  /\\ \\ / /\\ \\/ /',
        '| | | |  _| |  \\| | / /  \\ V /  \\  /',
        '| |_| | |___| |\\  |/ /_   | |   /  \\',
        '|____/|_____|_| \\_/____|  |_|  /_/\\_\\',
    ],
}

# batas panjang prompt (cukup untuk paste 10.000+ baris)
MAX_INPUT = 2_000_000

HELP_LINES = [
    "ctrl+p      command palette",
    "ctrl+x      leader: n sesi baru · l daftar sesi · b sidebar",
    "            m model · x export · c folder kerja · g daftar sesi",
    "ctrl+a      pilih model          ctrl+r  rename sesi",
    "ctrl+d      hapus sesi           ctrl+b  toggle sidebar",
    "ctrl+l      daftar sesi          ctrl+e  export chat",
    "ctrl+n      sesi baru            ctrl+s  simpan sesi",
    "ctrl+t      auto-allow tools     tab     toggle agent",
    "ctrl+k      tampil/sembunyi berpikir",
    "ctrl+u      menu utama (chat baru/riwayat)",
    "f2          ganti model (cycle)  Enter   kirim pesan",
    "enter kosong/spasi: lanjutkan kerja yang kepotong/stop",
    "shift+enter / alt+enter          baris baru",
    "esc         stop streaming + batal antrean (khusus)",
    "enter saat AI menjawab: pesan masuk ANTREAN (auto-kirim setelah selesai)",
    "ctrl+c      keluar",
    "pgup/pgdn   scroll · end bawah · wheel scroll",
    "klik pesan   menu aksi: salin / revert (undo) / hapus+prompt ulang / batal antrean",
    "slash: /help /clear /new /model /agent /session /menu",
    "       /export /rename /cwd",
    "system prompt: file system_prompt.md (edit = langsung aktif)",
    "tema: file theme.md (nama = fg,bg) · notifikasi selesai: DENZYX_NOTIFY=0",
]


def _session_files():
    if not SESSION_DIR.exists():
        return []
    return sorted(SESSION_DIR.glob("*.json"),
                  key=lambda f: f.stat().st_mtime, reverse=True)


def _session_meta(f):
    try:
        data = json.loads(f.read_text(encoding="utf-8"))
        return data.get("title", f.name), data.get("model", "?")
    except (OSError, json.JSONDecodeError):
        return f.name, "?"


def _blank_screen(h, w):
    """Hapus seluruh layar sekali — biar dialog/menu baru tidak numpuk di
    atas konten lama (ncurses cuma menulis sel yang disentuh)."""
    try:
        tmp = curses.newwin(h, w, 0, 0)
        tmp.clear()
        tmp.noutrefresh()
        curses.doupdate()
    except curses.error:
        pass


def dialog_note(stdscr, title, lines, footer="Enter/ESC: tutup"):
    """Dialog modal kecil untuk catatan / bantuan / hasil aksi."""
    h, w = stdscr.getmaxyx()
    _blank_screen(h, w)
    body = []
    for ln in "\n".join(lines).split("\n"):
        body.extend(wrap_text(ln, max(30, w - 10)))
    bh = min(len(body) + 4, max(8, h - 4))
    bw = min(max(44, w - 8), w - 2)
    win = curses.newwin(bh, bw, max(0, (h - bh) // 2), max(0, (w - bw) // 2))
    while True:
        win.erase()
        try:
            win.border(0)
            win.addstr(0, 2, f" {title} ", curses.A_BOLD)
            for i, ln in enumerate(body[: bh - 3]):
                win.addnstr(i + 1, 2, ln[: bw - 4], bw - 4)
            win.addstr(bh - 1, 2, footer[: bw - 4], curses.A_DIM)
        except curses.error:
            pass
        win.noutrefresh()
        stdscr.noutrefresh()
        curses.doupdate()
        while True:
            ch = _next_key()
            if ch in (_PASTE_START, _PASTE_END):
                continue
            break
        if ch in (10, 13, 27, curses.KEY_RESIZE):
            return


def palette_dialog(stdscr, state):
    """Command palette (ctrl+p) — kembali index aksi atau None."""
    items = [
        ("Sesi baru", "mulai percakapan kosong"),
        ("Daftar sesi", "buka sesi tersimpan"),
        ("Ganti model", "pilih model"),
        ("Rename sesi", "ubah judul sesi"),
        ("Ganti folder kerja", "folder aktif AI — tools bekerja di sini"),
        ("Hapus sesi", "hapus sesi aktif"),
        ("Ekspor chat", "simpan ke file markdown"),
        ("Toggle sidebar", "tampil/sembunyi panel sesi"),
        ("Toggle agent", "agent mode on/off"),
        ("Toggle reasoning", "tampilkan reasoning on/off"),
        ("Clear chat", "kosongkan percakapan ini"),
        ("Pengaturan", "temperature, key, dll"),
        ("Bantuan", "daftar pintasan"),
        ("Keluar", f"tutup {APP_NAME}"),
    ]
    sel = 0
    while True:
        ch, sel = menu_list(stdscr, " Command Palette ", items, sel,
                            f" model: {_model_label(state.model)} ",
                            " ↑↓ pilih • Enter: jalankan • ESC: batal ")
        if ch in (27,):
            return None
        if ch in (curses.KEY_UP, ord("k")):
            sel = (sel - 1) % len(items)
        elif ch in (curses.KEY_DOWN, ord("j")):
            sel = (sel + 1) % len(items)
        elif ch in (10, 13):
            return sel
        elif ch == curses.KEY_RESIZE:
            pass


def model_dialog(stdscr, state):
    items = [(_model_label(m), "✓" if m == state.model else "") for m in MODELS]
    sel = MODELS.index(state.model) if state.model in MODELS else 0
    while True:
        ch, sel = menu_list(stdscr, " Model ", items, sel, "",
                            " ↑↓ pilih • Enter: pakai • ESC: batal ")
        if ch in (27,):
            return None
        if ch in (curses.KEY_UP, ord("k")):
            sel = (sel - 1) % len(items)
        elif ch in (curses.KEY_DOWN, ord("j")):
            sel = (sel + 1) % len(items)
        elif ch in (10, 13):
            return MODELS[sel]
        elif ch == curses.KEY_RESIZE:
            pass


def export_chat(state):
    fname = Path(f"denzyx-export-{_wib_time():%Y%m%d-%H%M%S}.md")
    out = [f"# denzyx AI export — {state.session_title or 'untitled'}",
           f"model: {_model_label(state.model)} · agent: {'ON' if state.agent_mode else 'OFF'}"]
    for m in state.messages:
        role = m.get("role")
        content = m.get("content") or ""
        if role == "user" and content:
            out.append(f"\n## kamu\n\n{content}")
        elif role == "assistant" and content:
            out.append(f"\n## ai\n\n{content}")
        elif role == "tool":
            tcs = m.get("tool_calls") or []
            names = ", ".join((t.get("function") or {}).get("name", "?")
                              for t in tcs) or "tool"
            out.append(f"\n### tool: {names}\n")
    fname.write_text("\n".join(out), encoding="utf-8")
    return fname


# Telegram untuk laporan bug (t.me/colipopi)
BUG_URL = ("https://t.me/colipopi?text="
           "Halo%20DENZYX%2C%20saya%20menemukan%20bug%20di%20aplikasi"
           "%20denzyx%20AI")
BUG_LABEL = "t.me/colipopi"


def open_url(url):
    """Buka URL via termux-open-url (Termux) → xdg-open → open (macOS).
    Dijalankan di daemon thread supaya UI tidak pernah beku menunggu
    browser/WhatsApp. Return (ok, pesan_error): ok=True berarti command
    ditemukan & diluncurkan (hasil akhirnya tidak kita tunggu)."""
    import shutil
    import subprocess
    import threading
    on_termux = os.environ.get("PREFIX", "").startswith(
        "/data/data/com.termux")
    cmds = []
    if on_termux and shutil.which("termux-open-url"):
        cmds.append(["termux-open-url", url])
    for c in (["xdg-open", url], ["open", url]):
        if shutil.which(c[0]):
            cmds.append(c)
    if not cmds:
        return False, ("gagal buka link otomatis — butuh termux-api"
                       " (termux-open-url) atau xdg-open")

    def _run():
        for c in cmds:
            try:
                subprocess.run(c, timeout=8)
                return
            except Exception:
                continue

    threading.Thread(target=_run, daemon=True).start()
    return True, ""


def _copy_clipboard(text):
    """Salin ke clipboard: termux-clipboard-set → xclip → xsel → wl-copy
    → OSC 52 (fallback universal, jalan di Termux tanpa termux-api)."""
    import base64
    import shutil
    import subprocess
    # Hanya pakai termux-clipboard-set kalau BENAR-BENAR di Termux
    # (Android). Di luar Termux binary-nya menggantung menunggu API
    # service yang tidak ada → UI beku 3 detik. Cek env PREFIX.
    on_termux = os.environ.get("PREFIX", "").startswith(
        "/data/data/com.termux")
    if on_termux and shutil.which("termux-clipboard-set"):
        try:
            subprocess.run(["termux-clipboard-set"], input=text.encode(),
                           timeout=2, check=False)
            return True
        except Exception:  # noqa: BLE001
            pass
    for tool, args in (("xclip", ["xclip", "-selection", "clipboard"]),
                       ("xsel", ["xsel", "-b"]),
                       ("wl-copy", ["wl-copy"])):
        if shutil.which(tool):
            try:
                subprocess.run(args, input=text.encode(), timeout=3,
                               check=False)
                return True
            except Exception:  # noqa: BLE001
                pass
    # OSC 52: escape clipboard terminal — banyak emulator (Termux, iTerm,
    # kitty, tmux) menerimanya; tidak butuh paket tambahan.
    try:
        fd = os.open("/dev/tty", os.O_RDWR)
        try:
            os.write(fd, b"\x1b]52;c;" +
                     base64.b64encode(text.encode("utf-8")) + b"\x07")
        finally:
            os.close(fd)
        return True
    except OSError:
        return False


def chat_screen(stdscr, state):
    """Chat view: sidebar sesi + output + input + status bar."""
    global STYLE_ATTR
    curses.curs_set(0)
    msgs = []            # pesan mentah: [{"role":..., "text":...}]
    scroll = 0           # offset baris dari atas
    follow = True        # auto-scroll ke bawah
    streaming = False
    msg_queue = []       # antrean pesan (dikirim saat AI idle) — ala opencode
    pending_tools = None  # tool calls yang menunggu konfirmasi user
    active_tool = None    # nama tool yang sedang dijalankan AI (status bar)
    thinking = False      # AI sedang berpikir (reasoning) — status bar
    paste_mode = False   # bracketed paste aktif (teks mentah multi-baris)
    paste_changed = False
    pending_chars = 0
    paste_lines = 1
    paste_buf = bytearray()
    paste_len = 0
    last_paste_render = 0.0
    last_paste_activity = 0.0
    worker = None
    stop_evt = threading.Event()
    had_activity = False    # streaming menghasilkan konten/tool → notifikasi
    leave = False           # /menu → kembali ke menu utama
    out_q = queue.Queue()
    decision_q = queue.Queue()
    prompt_buf = ""
    wrap_cache = {}
    last_w = last_cw = last_h = -1
    win_side = win_out = win_in = win_status = None
    lines = []
    line_to_msg = []      # baris layar → index pesan (untuk klik)
    dirty = True
    sb_on = True            # sidebar sesi tampil
    sb_w = 26
    last_sb = -1
    sidebar = []            # [(path|None, title, model)]
    sb_sel = 0
    rain = _MatrixRain(1, 1)   # hujan matrix di latar sidebar sesi
    flash = ""
    flash_t = 0.0

    STYLE_ATTR = {
        "user":        curses.color_pair(2) | curses.A_BOLD,
        "user_label":  curses.color_pair(6) | curses.A_BOLD,
        "assistant":   curses.color_pair(3) | curses.A_BOLD,
        "ai_label":    curses.color_pair(7) | curses.A_BOLD,
        "reasoning":   curses.A_DIM | curses.color_pair(4),
        "reason_lbl":  curses.A_DIM | curses.color_pair(4),
        "error":       curses.color_pair(5),
        "error_label": curses.color_pair(9) | curses.A_BOLD,
        "tool":        curses.A_BOLD | curses.color_pair(1),
        "tool_label":  curses.color_pair(8) | curses.A_BOLD,
        "tool_out":    curses.A_DIM | curses.color_pair(4),
        "queued":      curses.A_DIM | curses.color_pair(3),
        "queued_lbl":  curses.color_pair(3) | curses.A_BOLD,
        "sep":         curses.A_DIM,
        "md_heading":  curses.A_BOLD | curses.color_pair(10),
        "md_code":     curses.color_pair(11),
        "md_bullet":   curses.color_pair(12),
        "md_quote":    curses.color_pair(13),
    }

    md_seg_cache = {}

    def set_flash(t):
        nonlocal flash, flash_t
        flash = t
        flash_t = time.time()

    def load_msgs():
        msgs.clear()
        for m in state.messages:
            if m["role"] == "user":
                msgs.append({"role": "user", "text": m["content"]})
            elif m["role"] == "assistant" and m["content"]:
                rc = m.get("reasoning_content")
                if rc:
                    msgs.append({"role": "reasoning", "text": rc})
                msgs.append({"role": "assistant", "text": m["content"]})

    load_msgs()

    def refresh_sidebar():
        nonlocal sb_sel
        s = [(None, "＋  Sesi Baru", "")]
        for f in _session_files():
            t, m = _session_meta(f)
            s.append((str(f), t[:22], m))
        sidebar[:] = s
        sb_sel = 0
        if state.saved_id:
            for i, (p, _, _) in enumerate(s):
                if p and os.path.abspath(p) == os.path.abspath(state.saved_id):
                    sb_sel = i
                    break

    def commit_paste():
        nonlocal prompt_buf, paste_mode, paste_buf, paste_len, \
            paste_lines, paste_changed
        prompt_buf += paste_buf.decode("utf-8", "replace")
        # kalau \x1b[201~ ikut tertelan (write parsial ke pty): buang sisa
        if prompt_buf.endswith("[201~"):
            prompt_buf = prompt_buf[:-5]
        paste_buf = bytearray()
        paste_len = 0
        paste_lines = 1
        paste_mode = False
        paste_changed = False

    refresh_sidebar()  # sidebar langsung terisi (sebelumnya kosong sampai event "done")

    def new_session():
        nonlocal prompt_buf, dirty, follow
        if state.messages:
            state.save_session()
        state.messages = []
        state.session_title = ""
        state.saved_id = None
        prompt_buf = ""
        load_msgs()
        refresh_sidebar()
        follow = True
        dirty = True

    def toggle_sidebar():
        nonlocal sb_on, dirty
        sb_on = not sb_on
        dirty = True

    def cycle_model():
        try:
            i = MODELS.index(state.model)
        except ValueError:
            i = -1
        state.model = MODELS[(i + 1) % len(MODELS)]
        set_flash(f"model → {_model_label(state.model)}")

    def do_rename():
        val = input_line(stdscr, "Judul sesi:", state.session_title)
        if val and val != "RESIZE":
            state.session_title = val
            state.save_session()
            refresh_sidebar()
            set_flash("judul sesi diperbarui")

    def do_delete():
        nonlocal dirty, follow
        if not confirm(stdscr, "Hapus sesi aktif beserta filenya?"):
            return
        if state.saved_id:
            try:
                Path(state.saved_id).unlink()
            except OSError:
                pass
        state.messages = []
        state.session_title = ""
        state.saved_id = None
        load_msgs()
        refresh_sidebar()
        follow = True
        dirty = True
        set_flash("sesi dihapus")

    def do_copy(idx):
        """Salin teks pesan ke clipboard."""
        if idx < 0 or idx >= len(msgs):
            return
        text = msgs[idx]["text"]
        if not text.strip():
            set_flash("pesan kosong — tidak ada yang disalin")
            return
        ok = _copy_clipboard(text)
        set_flash("pesan disalin ke clipboard ✓" if ok
                  else "gagal salin: butuh termux-api / xclip / wl-copy")

    def _prompt_of(idx):
        """Cari teks prompt user terdekat sebelum/termasuk idx."""
        for i in range(idx, -1, -1):
            if msgs[i]["role"] == "user":
                return msgs[i]["text"]
        return None

    def _state_idx(idx):
        """Jumlah baris user/assistant SEBELUM idx — itulah berapa entri
        state.messages yang tetap dipertahankan (reasoning/tool hanya
        tampilan, tidak ada di state)."""
        n = 0
        for i in range(idx):
            if msgs[i]["role"] in ("user", "assistant"):
                n += 1
        return n

    def do_delete_reprompt(idx):
        """Hapus pesan ini & semua sesudahnya, lalu muat prompt user
        terdekat ke input utk dikirim ulang."""
        nonlocal streaming, follow, scroll, prompt_buf, dirty
        if streaming:
            set_flash("tunggu jawaban selesai dulu sebelum hapus")
            return
        if idx < 0 or idx >= len(msgs):
            return
        p = _prompt_of(idx)
        n = _state_idx(idx)          # hitung SEBELUM msgs dipotong
        # (sama seperti revert: buang juga baris PIKIR/tool milik jawaban)
        idx_eff = idx
        if idx_eff > 0 and msgs[idx]["role"] == "assistant":
            while (idx_eff > 0
                   and msgs[idx_eff - 1]["role"]
                   in ("reasoning", "tool", "tool_out")):
                idx_eff -= 1
        del msgs[idx_eff:]
        if state.messages:
            del state.messages[n:]
        # bersihkan sisa assistant kosong (label tanpa isi)
        while (msgs and msgs[-1]["role"] == "assistant"
               and not msgs[-1]["text"].strip()):
            msgs.pop()
            if (state.messages and state.messages[-1]["role"] == "assistant"
                    and not state.messages[-1]["content"].strip()):
                state.messages.pop()
        state.save_session()
        follow = True
        scroll = 0
        dirty = True
        if p:
            prompt_buf = p
            set_flash("dihapus — prompt siap dikirim ulang (Enter) ✓")
        else:
            set_flash("pesan dihapus ✓")

    def do_revert(idx):
        """Revert ala opencode: pesan ini & semua sesudahnya di-undo
        (dihapus dari percakapan). Prompt user tetap tampil di chat dan
        dimuat ke input utk diperbaiki."""
        nonlocal streaming, follow, scroll, dirty, prompt_buf
        if streaming:
            set_flash("tunggu jawaban selesai dulu sebelum revert")
            return
        if idx < 0 or idx >= len(msgs):
            return
        p = _prompt_of(idx)
        n = _state_idx(idx)          # entri state yang dipertahankan
        # Undo pesan JAWABAN juga harus membuang baris miliknya di atasnya
        # (PIKIR / tool / tool_out) — kalau tidak, PIKIR tanpa jawaban
        # tetap tampil dan terasa seperti "AI tidak menjawab".
        idx_eff = idx
        if idx_eff > 0 and msgs[idx]["role"] == "assistant":
            while (idx_eff > 0
                   and msgs[idx_eff - 1]["role"]
                   in ("reasoning", "tool", "tool_out")):
                idx_eff -= 1
        del msgs[idx_eff:]
        if state.messages:
            del state.messages[n:]
        # bersihkan sisa assistant kosong (label tanpa isi)
        while (msgs and msgs[-1]["role"] == "assistant"
               and not msgs[-1]["text"].strip()):
            msgs.pop()
            if (state.messages and state.messages[-1]["role"] == "assistant"
                    and not state.messages[-1]["content"].strip()):
                state.messages.pop()
        state.save_session()
        if p:
            prompt_buf = p
        follow = True
        scroll = 0
        dirty = True
        set_flash("revert: di-undo — prompt tetap di chat & siap diedit ✓")

    def open_msg_menu(idx):
        """Menu aksi pesan (klik): Salin / Undo / Batal / Edit. Item bisa dipilih
        dengan tombol ATAU klik langsung (HP)."""
        nonlocal dirty, prompt_buf
        if idx < 0 or idx >= len(msgs):
            return
        m = msgs[idx]
        who = {"user": "KAMU", "assistant": "AI DENZYX",
               "reasoning": "PIKIR", "tool": "TOOL",
               "tool_out": "TOOL", "error": "ERROR",
               "queued": "ANTREAN"}.get(m["role"], "PESAN")
        if m["role"] == "queued":
            # pesan antrean: bisa diedit (muat ke input) atau dibatalkan
            items = [
                ("✏️ Edit", "muat pesan ini ke input utk diedit"),
                ("❌ Batal antrean", "hapus pesan ini dari antrean"),
                ("Batal", "tutup menu"),
            ]
        else:
            items = [
                ("📋 Salin", "salin teks pesan ke clipboard"),
                ("↩️ Undo", "undo pesan ini & sesudahnya"),
                ("🗑 Hapus", "hapus & prompt ulang dari awal"),
                ("Batal", "tutup menu"),
            ]
        sel = 0
        while True:
            ch, sel = menu_list(stdscr, f" {who} — pilih aksi ", items, sel,
                                "", " ↑↓ pilih • Enter: jalankan • ESC: batal ")
            if ch == 27:
                return
            if ch in (curses.KEY_UP, ord("k")):
                sel = (sel - 1) % len(items)
            elif ch in (curses.KEY_DOWN, ord("j")):
                sel = (sel + 1) % len(items)
            elif ch in (10, 13):
                break
            elif ch == curses.KEY_RESIZE:
                pass
        if sel == 0:
            if m["role"] == "queued":
                # muat pesan antrean kembali ke input untuk diedit
                for i, qm in enumerate(msg_queue):
                    if qm == m["text"]:
                        del msg_queue[i]
                        break
                for i in range(len(msgs) - 1, -1, -1):
                    if msgs[i]["role"] == "queued" and msgs[i]["text"] == m["text"]:
                        del msgs[i]
                        break
                prompt_buf = m["text"]
                dirty = True
                set_flash("✏️ antrean dibuka ke input — Enter utk kirim ✓")
            else:
                do_copy(idx)
        elif sel == 1:
            if m["role"] == "queued":
                # batal satu pesan antrean
                for i, qm in enumerate(msg_queue):
                    if qm == m["text"]:
                        del msg_queue[i]
                        break
                for i in range(len(msgs) - 1, -1, -1):
                    if msgs[i]["role"] == "queued" and msgs[i]["text"] == m["text"]:
                        del msgs[i]
                        break
                dirty = True
                set_flash("pesan dikeluarkan dari antrean ✓")
            else:
                do_revert(idx)
        elif sel == 2:
            if m["role"] != "queued":
                do_delete_reprompt(idx)

    def do_export():
        try:
            fname = export_chat(state)
            dialog_note(stdscr, " Ekspor ", ["Chat disimpan ke:", str(fname)])
        except OSError as e:
            dialog_note(stdscr, " Ekspor ", [f"Gagal menulis file: {e}"])

    def send_prompt(text):
        """Kirim prompt ke API: append user+assistant, mulai worker baru.
        Dipakai Enter langsung ATAU drain antrean (auto-kirim)."""
        nonlocal streaming, follow, dirty
        # buang event basi dari stream yang di-stop (ESC)
        try:
            while True:
                out_q.get_nowait()
        except queue.Empty:
            pass
        # hapus baris antrean yang sedang dikirim (kalau dari antrean)
        for i in range(len(msgs) - 1, -1, -1):
            if msgs[i]["role"] == "queued":
                del msgs[i]
                break
        msgs.append({"role": "user", "text": text})
        msgs.append({"role": "assistant", "text": ""})
        state.messages.append({"role": "user", "content": text})
        state.messages.append({"role": "assistant", "content": ""})
        follow = True
        dirty = True
        streaming = True
        if state.agent_mode:
            worker = threading.Thread(
                target=stream_agent,
                args=(state, text, out_q, decision_q, stop_evt), daemon=True)
        else:
            worker = threading.Thread(
                target=stream_chat, args=(state, text, out_q, stop_evt), daemon=True)
        worker.start()

    def drain_queue():
        """Auto-kirim antrean satu per satu saat AI idle (ala opencode).
        Perintah /… yang terlanjur masuk antrean dijalankan sebagai
        perintah (bukan dikirim sebagai teks mentah)."""
        nonlocal streaming, dirty
        if streaming or not msg_queue:
            return
        msg = msg_queue.pop(0)
        if msg.startswith("/") and "\n" not in msg and not msg.startswith("//"):
            # jalankan sebagai perintah setelah idle
            for i in range(len(msgs) - 1, -1, -1):
                if msgs[i]["role"] == "queued" and msgs[i]["text"] == msg:
                    del msgs[i]
                    break
            dirty = True
            do_slash(msg)
            if msg_queue:
                set_flash(f"📥 perintah dijalankan — {len(msg_queue)} tersisa")
            else:
                set_flash("📥 antrean selesai ✓")
            return
        send_prompt(msg)
        if msg_queue:
            set_flash(f"📥 antrean terkirim — {len(msg_queue)} tersisa")
        else:
            set_flash("📥 antrean terkirim ✓")

    def cancel_queue():
        """Batal semua antrean (ESC saat idle)."""
        nonlocal dirty
        msg_queue.clear()
        for i in range(len(msgs) - 1, -1, -1):
            if msgs[i]["role"] == "queued":
                del msgs[i]
        dirty = True
        set_flash("antrean dibatalkan ✓")

    def do_slash(cmd):
        nonlocal dirty, follow, leave
        c, _, arg = cmd.partition(" ")
        c = c.lower()
        if c == "/help":
            dialog_note(stdscr, f" Bantuan {APP_NAME} ", HELP_LINES)
        elif c == "/clear":
            if state.messages and confirm(stdscr, "Kosongkan percakapan ini?"):
                state.messages = []
                load_msgs()
                follow = True
                dirty = True
                state.save_session()
        elif c == "/new":
            new_session()
        elif c == "/model":
            if arg in MODELS:
                state.model = arg
                set_flash(f"model → {_model_label(state.model)}")
            else:
                m = model_dialog(stdscr, state)
                if m:
                    state.model = m
                    set_flash(f"model → {_model_label(state.model)}")
        elif c == "/agent":
            state.agent_mode = not state.agent_mode
            set_flash(f"agent mode: {'ON' if state.agent_mode else 'OFF'}")
        elif c == "/session":
            history_screen(stdscr, state)
            load_msgs()
            refresh_sidebar()
            follow = True
            dirty = True
        elif c == "/export":
            do_export()
        elif c == "/cwd":
            val = input_line(stdscr, "Folder kerja (path):", str(state.cwd))
            if val and val != "RESIZE":
                p = Path(os.path.expanduser(val.strip()))
                if p.is_dir():
                    state.cwd = p
                    set_flash(f"folder kerja → {p}")
                else:
                    set_flash("folder tidak ada — folder tetap")
        elif c == "/menu":
            # kembali ke menu utama (chat baru / riwayat / pengaturan)
            if streaming:
                stop_evt.set()
                if worker:
                    worker.join(timeout=1)
            state.save_session()
            leave = True
        elif c == "/system":
            dialog_note(stdscr, " System Prompt ",
                        [f"Dimuat dari file: {SYSTEM_PROMPT_FILE}",
                         "Edit file lalu restart app.",
                         "Status: " + ("terisi" if state.system else "kosong")])
        elif c == "/rename":
            do_rename()
        elif c == "/theme":            dialog_note(stdscr, " Tema ",
                        [f"{APP_NAME} memakai tema bawaan terminal.",
                         "Warna: Pengaturan > Pengaturan."])
        elif c == "/compact":
            dialog_note(stdscr, " Kompaksi ",
                        ["Kompaksi belum didukung.",
                         "Sesi disimpan utuh di ~/.denzyx_sessions."])
        else:
            dialog_note(stdscr, " Perintah tidak dikenal ",
                        [f"'{cmd}' bukan perintah.", "Ketik /help untuk daftar."])
        dirty = True

    def build_lines(width):
        """Wrap ulang semua pesan sesuai lebar terminal saat ini.
        Pesan user/assistant di-highlight markdown (heading/kode/bullet/
        kutipan + inline `kode` & **tebal**).
        Return (lines, line_to_msg): line_to_msg[i] = index pesan yang
        menampung baris ke-i (untuk klik → pesan)."""
        lines = []
        line_to_msg = []
        sep = "─" * max(10, width)
        for mi, m in enumerate(msgs):
            role, text = m["role"], m["text"]
            if role == "reasoning" and not state.show_reasoning:
                continue  # PIKIR disembunyikan — toggle ctrl+k munculkan lagi
            ck = (role, text, width)
            if ck not in wrap_cache:
                if role in ("user", "assistant"):
                    label = (("user_label", "── KAMU ──") if role == "user"
                             else ("ai_label", "── AI DENZYX ──"))
                    block = [label]
                    for kind, ln in md_blocks(text):
                        sk = role if kind == "plain" else kind
                        block += [(sk, x) for x in wrap_text(ln, width)]
                else:
                    wrapped = wrap_text(text, width)
                    if role == "reasoning":
                        block = [("reason_lbl", "── PIKIR ──")]
                        block += [("reasoning", x) for x in wrapped]
                    elif role == "error":
                        block = [("error_label", "── ERROR ──")]
                        block += [("error", x) for x in wrapped]
                    elif role == "tool":
                        block = [("tool_label", "── TOOL ──")]
                        block += [("tool", x) for x in wrapped]
                    elif role == "tool_out":
                        block = [("tool_out", x) for x in wrapped]
                    elif role == "queued":
                        block = [("queued_lbl", "── 📥 ANTREAN ──")]
                        block += [("queued", x) for x in wrapped]
                    else:
                        block = [("ai_label", "── AI DENZYX ──")]
                        block += [("assistant", x) for x in wrapped]
                wrap_cache[ck] = block
            lines.extend(wrap_cache[ck])
            line_to_msg.extend([mi] * len(wrap_cache[ck]))
            lines.append(("sep", sep))
            line_to_msg.append(mi)
        return lines, line_to_msg

    def render_sidebar():
        win_side.erase()
        sh, sw = win_side.getmaxyx()
        rain.resize(sh, sw)
        rain.draw(win_side)
        draw_frame(win_side, f" {APP_NAME} ")
        maxitems = max(1, sh - 4)
        start = 0
        if len(sidebar) > maxitems:
            start = min(max(0, sb_sel - 1), len(sidebar) - maxitems)
        y = 2
        for i in range(start, min(len(sidebar), start + maxitems)):
            if i == sb_sel:
                marker, attr = "▶", curses.A_BOLD | curses.color_pair(1)
            else:
                marker, attr = " ", 0
            label = sidebar[i][1]
            try:
                win_side.addstr(y, 1, f"{marker} {label[: sw - 4]}", attr)
            except curses.error:
                pass
            y += 1
        try:
            win_side.addnstr(sh - 1, 1, "ctrl+b: sembunyi · ↑↓ pilih",
                             sw - 2, curses.A_DIM)
        except curses.error:
            pass
        win_side.noutrefresh()

    def render_status():
        win_status.erase()
        try:
            left = clip_width(" " + (state.cwd.name or "/"), 14)
            win_status.addnstr(0, 1, left + " ", w - 1, curses.A_DIM)
            right = (f" ctrl+p · ctrl+x · /help"
                     f" · DENZYX · {w}x{h} ")
            mid = ""
            q = f"📥 {len(msg_queue)} antrean · " if msg_queue else ""
            if streaming:
                if active_tool:
                    base = f"🔧 {active_tool}"
                elif thinking:
                    base = "⋯ AI berpikir…"
                else:
                    base = "⋯ AI mengetik…"
                if not follow and max_scroll > 0:
                    pct = int(scroll / max_scroll * 100)
                    mid = f"{base} · ^ {pct}% — End: bawah"
                else:
                    mid = f"{base} — esc stop"
            elif not follow and max_scroll > 0:
                pct = int(scroll / max_scroll * 100)
                mid = f"^ scroll {scroll}/{max_scroll} ({pct}%) — End: bawah"
            else:
                mid = state.session_title or "sesi baru"
            if q:
                mid = q + mid
            if mid:
                # mid di KIRI, right di KANAN — potong mid agar tak tertimpa
                mid_attr = (curses.A_BOLD | curses.color_pair(3)
                            if streaming else curses.A_DIM)
                avail = max(4, w - len(right) - len(left) - 3)
                win_status.addnstr(0, len(left) + 2,
                                   f" {clip_width(mid, avail)} ",
                                   w - 1, mid_attr)
            win_status.addnstr(0, w - len(right) - 1, right, w - 1,
                               curses.A_DIM)
        except curses.error:
            pass
        win_status.noutrefresh()

    def render_input():
        try:
            in_h, _ = win_in.getmaxyx()
            body_h = max(1, in_h - 2)
            win_in.erase()
            if flash and time.time() - flash_t < 3.0:
                # flash tampil di judul frame input — SELALU terlihat,
                # tidak tergantung lebar terminal (status bar bisa penuh)
                draw_frame(win_in, f" {flash} ", "")
            elif paste_mode:
                draw_frame(win_in, " [PASTE] teks masuk apa adanya — ESC: batal ", "")
            else:
                draw_frame(win_in, " input — Enter kirim · Shift+Enter baris baru ", "")
            tail_str = prompt_buf
            if paste_buf:
                tail_str += paste_buf[-400:].decode("utf-8", "replace")
            if len(tail_str) > 400:
                tail_str = tail_str[-400:]
            tail = tail_str.rsplit("\n", body_h + 6)
            rendered = []
            for ln in tail:
                rendered.extend(wrap_text(ln, max(10, w - sb - 6)) or [""])
            shown = rendered[-(body_h):] if rendered else [""]
            y = 1
            for i, ln in enumerate(shown):
                if i == len(shown) - 1:
                    win_in.addnstr(y, 1, clip_width("> " + ln, w - sb - 2),
                                   w - sb - 2)
                else:
                    win_in.addnstr(y, 1, clip_width(ln, w - sb - 2), w - sb - 2)
                y += 1
        except curses.error:
            pass

    while True:
        if leave:
            return  # /menu atau ctrl+u → kembali ke menu utama
        h, w = stdscr.getmaxyx()
        if h < 12 or w < 60:
            stdscr.addstr(0, 0, "Terminal terlalu kecil (min 60x12)")
            stdscr.refresh()
            _next_key()
            return
        sb = sb_w if sb_on else 0
        out_h = h - 8
        content_w = max(10, w - sb - 4)
        if (win_out is None or h != last_h or w != last_w or sb != last_sb):
            win_side = curses.newwin(max(1, h - 1), sb_w, 0, 0)
            win_out = curses.newwin(out_h, max(1, w - sb), 0, sb)
            win_in = curses.newwin(6, max(1, w - sb), h - 7, sb)
            win_status = curses.newwin(1, w, h - 1, 0)
            last_h, last_w, last_sb = h, w, sb
            dirty = True

        # render ringan saat paste: hanya input+status, maks tiap 80ms atau
        # tiap 2048 chars — render penuh per char = lag untuk paste besar
        now = time.time()
        light = (
            paste_mode and paste_changed and
            (now - last_paste_render >= 0.08 or pending_chars >= 2048)
        )
        if light:
            last_paste_render = now
            pending_chars = 0
            paste_changed = False

        full = dirty or last_cw != content_w or not paste_mode
        if full:
            lines, line_to_msg = build_lines(content_w)
            last_cw = content_w
            dirty = False

            win_out.erase()

            visible = out_h
            total = len(lines)
            max_scroll = max(0, total - visible)
            # follow saja yang memaksa ke bawah — saat streaming user
            # tetap bisa scroll naik untuk baca pesan lama
            if follow:
                scroll = max_scroll
            scroll = max(0, min(scroll, max_scroll))

            y = 0
            for idx in range(scroll, min(total, scroll + visible)):
                style, text = lines[idx]
                attr = STYLE_ATTR.get(style, 0)
                try:
                    if style in ("user", "assistant", "md_quote"):
                        ckey = (style, text)
                        segs = md_seg_cache.get(ckey)
                        if segs is None:
                            segs = md_inline_segs(text, attr)
                            if segs is None:
                                segs = []
                            md_seg_cache[ckey] = segs
                        if segs:
                            x = 1
                            for seg, a in segs:
                                sw = sum(2 if unicodedata.east_asian_width(c)
                                         in ("W", "F") else 1 for c in seg)
                                if x + sw > w - sb - 1:
                                    break
                                win_out.addnstr(y, x, seg, w - sb - 1 - x, a)
                                x += sw
                        else:
                            win_out.addnstr(y, 1,
                                            clip_width(text, w - sb - 2),
                                            w - sb - 2, attr)
                    else:
                        win_out.addnstr(y, 1, clip_width(text, w - sb - 2),
                                        w - sb - 2, attr)
                except curses.error:
                    pass
                y += 1
        elif light:
            pass

        visible = out_h
        total = len(lines)
        max_scroll = max(0, total - visible)

        # sidebar + status + input selalu di-render (murah, status berubah
        # tiap saat: flash, scroll %, streaming)
        stdscr.noutrefresh()
        if sb_on:
            render_sidebar()
        render_status()
        render_input()
        win_in.noutrefresh()
        win_out.noutrefresh()
        curses.doupdate()

        if streaming:
            # proses queue
            got = False
            try:
                while True:
                    kind, text = out_q.get_nowait()
                    got = True
                    if kind == "content":
                        thinking = False  # AI mulai mengetik jawaban
                        had_activity = True
                        if (msgs and msgs[-1]["role"] in
                                ("tool", "tool_out", "reasoning")):
                            # sambung ke pesan assistant terakhir — jangan
                            # bikin label AI DENZYX ganda (kesan ngespam)
                            for _i in range(len(msgs) - 1, -1, -1):
                                if msgs[_i]["role"] == "assistant":
                                    msgs[_i]["text"] += text
                                    break
                            else:
                                msgs.append({"role": "assistant", "text": text})
                        else:
                            msgs[-1]["text"] += text
                        if state.messages and state.messages[-1]["role"] == "assistant":
                            state.messages[-1]["content"] += text
                        dirty = True
                    elif kind == "reasoning":
                        thinking = True   # AI sedang berpikir
                        # simpan ke sesi — thinking mode mewajibkan
                        # reasoning_content di-pass-back ke API saat resume
                        if (state.messages
                                and state.messages[-1].get("role")
                                == "assistant"):
                            state.messages[-1]["reasoning_content"] = (
                                state.messages[-1].get(
                                    "reasoning_content", "") + text)
                        # selalu disimpan di msgs — toggle ctrl+k cukup
                        # menyembunyikan/memunculkan (filter di render).
                        # API ini kadang kirim reasoning SETELAH content →
                        # sisipkan PIKIR DI ATAS pesan assistant biar urutan
                        # tampil: pikir dulu, baru jawab.
                        if msgs and msgs[-1]["role"] == "reasoning":
                            msgs[-1]["text"] += text
                        elif msgs and msgs[-1]["role"] == "assistant":
                            # sisipkan PIKIR di atas pesan assistant; kalau
                            # baris reasoning sudah ada tepat di atasnya
                            # (stream selang-seling reason/content), gabung
                            if len(msgs) >= 2 and msgs[-2]["role"] == "reasoning":
                                msgs[-2]["text"] += text
                            else:
                                msgs.insert(-1, {"role": "reasoning", "text": text})
                        else:
                            msgs.append({"role": "reasoning", "text": text})
                        dirty = True
                    elif kind == "tool_pending":
                        # sinkron dgn state: buang stub assistant kosong
                        thinking = False
                        if (msgs and msgs[-1]["role"] == "assistant"
                                and not msgs[-1]["text"]):
                            msgs.pop()
                        for c in text:
                            args_txt = c["arguments"][:160] or "{}"
                            msgs.append({"role": "tool",
                                         "text": f"{c['name']} {args_txt}"})
                        if len(text) > 1:
                            active_tool = f"{text[0]['name']} +{len(text) - 1}"
                        else:
                            active_tool = text[0]["name"] if text else None
                        pending_tools = text
                        follow = True
                        dirty = True
                    elif kind == "tool_result":
                        _name, _args, result = text
                        active_tool = None  # tool selesai dijalankan
                        had_activity = True
                        shown = result.replace("\n", " ")[:400]
                        msgs.append({"role": "tool_out", "text": f"-> {shown}"})
                        dirty = True
                    elif kind == "note":
                        msgs.append({"role": "reasoning", "text": text})
                        dirty = True
                    elif kind == "error":
                        active_tool = None
                        thinking = False
                        msgs.append({"role": "error", "text": text})
                        dirty = True
                    elif kind == "done":
                        streaming = False
                        pending_tools = None
                        active_tool = None
                        thinking = False
                        follow = True
                        dirty = True
                        if had_activity and not stop_evt.is_set():
                            notify_done()
                        had_activity = False
                        state.save_session()
                        refresh_sidebar()
                        drain_queue()   # antrean dikirim otomatis saat idle
            except queue.Empty:
                pass
            if got:
                continue
            # non-blocking saat streaming: proses antrian terus-terusan
            ch = _next_key(0.05)
        else:
            # idle: tunggu input (blocking, dari thread reader).
            # Saat paste mode: non-blocking + timeout — kalau 27 dari
            # \x1b[201~ hilang (write terminal parsial ke pty), paste
            # tetap dikomit otomatis, bukan nyangkut selamanya.
            # Saat sidebar tampil: timeout pendek biar hujan matrix hidup.
            ch = _next_key(0.06 if sb_on else (0.25 if paste_mode else -1))

        if ch is None:
            if sb_on:
                rain.step()
                dirty = True
            # paste macet (kalau \x1b[201~ hilang krn write parsial) →
            # commit otomatis setelah 2 dtk tanpa karakter baru
            if paste_mode and time.monotonic() - last_paste_activity > 2.0:
                commit_paste()
                dirty = True
            continue

        last_paste_activity = time.monotonic()

        if pending_tools is not None:
            # mode konfirmasi tool — input prompt nonaktif
            if ch in (ord("y"), ord("Y")):
                decision_q.put("y")
                pending_tools = None
            elif ch in (ord("a"), ord("A")):
                decision_q.put("a")
                pending_tools = None
            elif ch in (ord("n"), ord("N"), 27):
                decision_q.put("n")
                pending_tools = None
            continue

        # bracketed paste / ESC asli: diparsing manual di _next_key
        if ch == _PASTE_START:
            paste_mode = True
            paste_changed = False
            pending_chars = 0
            paste_lines = 1
            paste_buf = bytearray()
            paste_len = 0
            last_paste_render = 0.0
            dirty = True
            continue
        if ch == _PASTE_END:
            if paste_mode:
                commit_paste()
                dirty = True
            continue
        if paste_mode:
            # semua karakter paste masuk apa adanya; newline = bagian teks.
            # Ditampung di bytearray dulu (concat string per char = O(n²)).
            if ch in (10, 13):
                if paste_len < MAX_INPUT:
                    paste_buf.append(10)
                    paste_len += 1
                    paste_lines += 1
                paste_changed = True
                pending_chars += 1
            elif ch in (curses.KEY_BACKSPACE, 127, 8):
                if paste_buf:
                    if paste_buf[-1] == 10:
                        paste_lines = max(1, paste_lines - 1)
                    paste_buf.pop()
                    paste_len = max(0, paste_len - 1)
                else:
                    prompt_buf = prompt_buf[:-1]
                paste_changed = True
                pending_chars += 1
            elif ch == curses.KEY_MOUSE:
                # scroll tetap berfungsi saat paste (jangan masuk buffer!)
                try:
                    _, _, _, _, bstate = curses.getmouse()
                    if bstate & curses.BUTTON4_PRESSED:      # wheel up
                        follow = False
                        scroll = max(0, scroll - 3)
                    elif bstate & curses.BUTTON5_PRESSED:    # wheel down
                        scroll = min(max_scroll, scroll + 3)
                        if scroll >= max_scroll:
                            follow = True
                except curses.error:
                    pass
            elif ch == curses.KEY_RESIZE:
                dirty = True
            elif 32 <= ch < 127 or 127 < ch <= 255:
                # byte teks asli (ASCII + UTF-8) — special key ncurses
                # (>255: panah, PgUp/PgDn, dll) DIABAIKAN, bukan crash.
                if paste_len < MAX_INPUT:
                    paste_buf.append(ch)
                    paste_len += 1
                paste_changed = True
                pending_chars += 1
            continue

        if ch == 27:
            # ESC: stop streaming + batalkan antrean (kalau ada). Tidak
            # bersihkan input, tidak balik ke menu.
            if streaming:
                streaming = False
                stop_evt.set()
                if worker is not None:
                    try:
                        worker.join(timeout=1)
                    except Exception:  # noqa: BLE001
                        pass
                state.save_session()
                if msg_queue:
                    cancel_queue()
                    set_flash("streaming dihentikan — antrean dibatalkan")
                else:
                    set_flash("streaming dihentikan")
                dirty = True
            continue
        if ch == 3:
            # ctrl+c — kalau lolos dari ISIG terminal: keluar app
            raise KeyboardInterrupt

        # --- pintasan utama (non-paste) ---
        if ch == 24:
            # ctrl+x leader — tunggu 1.5 dtk utk key kedua
            lc = _next_key(1.5)
            if lc in (None, 27):
                set_flash("leader dibatalkan")
            elif lc in (ord("n"), ord("N")):
                new_session()
            elif lc in (ord("l"), ord("L"), ord("g"), ord("G")):
                history_screen(stdscr, state)
                load_msgs()
                refresh_sidebar()
                follow = True
                dirty = True
            elif lc in (ord("b"), ord("B")):
                toggle_sidebar()
            elif lc in (ord("m"), ord("M")):
                m = model_dialog(stdscr, state)
                if m:
                    state.model = m
                    set_flash(f"model → {_model_label(state.model)}")
                dirty = True
            elif lc in (ord("x"), ord("X")):
                do_export()
            elif lc in (ord("t"), ord("T")):
                dialog_note(stdscr, " Tema ",
                            [f"{APP_NAME} memakai tema bawaan terminal."])
            elif lc in (ord("e"), ord("E")):
                dialog_note(stdscr, " Editor ",
                            ["Editor eksternal belum didukung.",
                             "Gunakan paste (Ctrl+Shift+V) untuk teks panjang."])
            elif lc in (ord("c"), ord("C")):
                val = input_line(stdscr, "Folder kerja (path):",
                                 str(state.cwd))
                if val and val != "RESIZE":
                    p = Path(os.path.expanduser(val.strip()))
                    if p.is_dir():
                        state.cwd = p
                        set_flash(f"folder kerja → {p}")
                    else:
                        set_flash("folder tidak ada — folder tetap")
            dirty = True
            continue
        if ch == 16 and not streaming:      # ctrl+p command palette
            i = palette_dialog(stdscr, state)
            dirty = True
            if i == 0:
                new_session()
            elif i == 1:
                history_screen(stdscr, state)
                load_msgs()
                refresh_sidebar()
                follow = True
                dirty = True
            elif i == 2:
                m = model_dialog(stdscr, state)
                if m:
                    state.model = m
                    set_flash(f"model → {_model_label(state.model)}")
            elif i == 3:
                do_rename()
            elif i == 4:
                val = input_line(stdscr, "Folder kerja (path):",
                                 str(state.cwd))
                if val and val != "RESIZE":
                    p = Path(os.path.expanduser(val.strip()))
                    if p.is_dir():
                        state.cwd = p
                        set_flash(f"folder kerja → {p}")
                    else:
                        set_flash("folder tidak ada — folder tetap")
            elif i == 5:
                do_delete()
            elif i == 6:
                do_export()
            elif i == 7:
                toggle_sidebar()
            elif i == 8:
                state.agent_mode = not state.agent_mode
                set_flash(f"agent mode: {'ON' if state.agent_mode else 'OFF'}")
            elif i == 9:
                state.show_reasoning = not state.show_reasoning
                set_flash(f"reasoning: {'ON' if state.show_reasoning else 'OFF'}")
            elif i == 10:
                if state.messages and confirm(stdscr, "Kosongkan percakapan ini?"):
                    state.messages = []
                    load_msgs()
                    follow = True
                    dirty = True
            elif i == 11:
                settings_screen(stdscr, state)
                dirty = True
            elif i == 12:
                dialog_note(stdscr, f" Bantuan {APP_NAME} ", HELP_LINES)
            elif i == 13:
                raise KeyboardInterrupt
            continue
        if ch == 1:        # ctrl+a → pilih model
            m = model_dialog(stdscr, state)
            if m:
                state.model = m
                set_flash(f"model → {_model_label(state.model)}")
            dirty = True
            continue
        if ch == 18:       # ctrl+r → rename sesi
            do_rename()
            dirty = True
            continue
        if ch == 4:        # ctrl+d → hapus sesi
            do_delete()
            continue
        if ch == 2:        # ctrl+b → toggle sidebar
            toggle_sidebar()
            continue
        if ch == 12 and not streaming:   # ctrl+l → daftar sesi
            history_screen(stdscr, state)
            load_msgs()
            refresh_sidebar()
            follow = True
            dirty = True
            continue
        if ch == 20:       # ctrl+t → toggle auto-allow
            state.auto_allow = not state.auto_allow
            set_flash(f"auto-allow tools: {'ON' if state.auto_allow else 'OFF'}")
            dirty = True
            continue
        if ch == 11:       # ctrl+k → tampil/sembunyi berpikir
            state.show_reasoning = not state.show_reasoning
            set_flash(f"berpikir: {'muncul' if state.show_reasoning else 'disembunyikan'} "
                      "(ctrl+k untuk kembali)")
            dirty = True
            continue
        if ch == 21:       # ctrl+u → kembali ke menu utama
            if streaming:
                stop_evt.set()
                if worker:
                    worker.join(timeout=1)
            state.save_session()
            return
        if ch == 5 and not streaming:   # ctrl+e → export chat
            do_export()
            dirty = True
            continue
        if ch == 19 and not streaming:  # ctrl+s → simpan sesi sekarang
            state.save_session()
            refresh_sidebar()
            set_flash("sesi disimpan ✓")
            dirty = True
            continue
        if ch == 14 and not streaming:  # ctrl+n → sesi baru
            new_session()
            continue
        if ch == 9:        # tab → toggle agent
            state.agent_mode = not state.agent_mode
            set_flash(f"agent mode: {'ON' if state.agent_mode else 'OFF'}")
            continue
        if ch == curses.KEY_F2:    # f2 → ganti model
            cycle_model()
            continue
        if ch == curses.KEY_BTAB:
            continue
        if ch == curses.KEY_MOUSE:
            try:
                _, _, _, _, bstate = curses.getmouse()
                if bstate & curses.BUTTON4_PRESSED:      # wheel up
                    follow = False
                    scroll = max(0, scroll - 3)
                elif bstate & curses.BUTTON5_PRESSED:    # wheel down
                    scroll = min(max_scroll, scroll + 3)
                    if scroll >= max_scroll:
                        follow = True
            except curses.error:
                pass
            continue
        if ch == _KEY_CLICK:
            # klik pesan → menu aksi (salin / undo / batal) — HP friendly
            cx, cy = _click_pos
            if not paste_mode and 2 <= cy <= out_h - 1 and cx > sb:
                li = scroll + (cy - 2)
                if 0 <= li < len(line_to_msg):
                    open_msg_menu(line_to_msg[li])
            dirty = True
            continue
        if ch == curses.KEY_RESIZE:
            dirty = True
            continue
        if ch == curses.KEY_PPAGE:
            follow = False
            scroll = max(0, scroll - max(1, visible - 2))
            continue
        if ch == curses.KEY_NPAGE:
            scroll = min(max_scroll, scroll + max(1, visible - 2))
            if scroll >= max_scroll:
                follow = True
            continue
        if ch == curses.KEY_HOME:
            follow = False
            scroll = 0
            continue
        if ch == curses.KEY_END:
            follow = True
            continue
        if ch in (curses.KEY_UP, curses.KEY_DOWN):
            if not prompt_buf and not streaming and sb_on:
                # input kosong + sidebar tampil → navigasi daftar sesi
                if ch == curses.KEY_UP:
                    sb_sel = (sb_sel - 1) % len(sidebar)
                else:
                    sb_sel = (sb_sel + 1) % len(sidebar)
            else:
                # scroll output
                if ch == curses.KEY_UP:
                    follow = False
                    scroll = max(0, scroll - 1)
                else:
                    scroll = min(max_scroll, scroll + 1)
                    if scroll >= max_scroll:
                        follow = True
            continue
        if ch in (10, 13):
            if not prompt_buf:
                # input kosong + Enter → buka sesi dari sidebar
                if sb_on and not streaming:
                    path, _, _ = sidebar[sb_sel]
                    if path is None:
                        new_session()
                    else:
                        state.load_session(path)
                        load_msgs()
                        refresh_sidebar()
                        follow = True
                        dirty = True
                        set_flash("sesi dimuat")
                continue
            msg = prompt_buf.strip()
            if not msg:
                # Enter kosong / spasi doang → lanjutkan yang kepotong
                if streaming:
                    set_flash("sedang streaming — esc untuk stop")
                    continue
                if not state.messages:
                    set_flash("belum ada percakapan — ketik dulu")
                    continue
                last = state.messages[-1]
                resume_tool = (state.agent_mode
                               and last.get("role") == "assistant"
                               and last.get("tool_calls"))
                if not resume_tool:
                    msgs.append({"role": "user", "text": "⟳ lanjutkan…"})
                    msgs.append({"role": "assistant", "text": ""})
                    state.messages.append(
                        {"role": "user", "content": "Lanjutkan dari yang tadi."})
                    state.messages.append({"role": "assistant", "content": ""})
                prompt_buf = ""
                follow = True
                dirty = True
                # buang event basi dari stream yang di-stop (ESC)
                try:
                    while True:
                        out_q.get_nowait()
                except queue.Empty:
                    pass
                streaming = True
                if state.agent_mode:
                    worker = threading.Thread(
                        target=stream_agent,
                        args=(state,
                              None if resume_tool else "Lanjutkan dari yang tadi.",
                              out_q, decision_q, stop_evt),
                        kwargs={"resume": resume_tool}, daemon=True)
                else:
                    worker = threading.Thread(
                        target=stream_chat,
                        args=(state, "Lanjutkan dari yang tadi.",
                              out_q, stop_evt), daemon=True)
                worker.start()
                continue
            # cegah double-streaming: kalau AI masih menjawab, pesan TIDAK
            # diblokir — masuk antrean & auto-kirim setelah jawaban selesai
            # (fitur QUEUED ala opencode). Teks tampil di chat sebagai
            # baris "📥 ANTREAN". Perintah /... tidak ikut diantre —
            # tunggu sampai idle (slash saat streaming jadi teks mentah).
            if streaming:
                if msg.startswith("/") and "\n" not in msg \
                        and not msg.startswith("//"):
                    set_flash("tunggu jawaban selesai utk perintah " + msg.split()[0])
                    continue
                msg_queue.append(msg)
                msgs.append({"role": "queued", "text": msg})
                prompt_buf = ""
                follow = True
                dirty = True
                set_flash("📥 masuk antrean — auto-kirim setelah jawaban ✓")
                continue
            # slash command: hanya kalau satu baris (paste multi-baris
            # yang diawali "/" tidak boleh tertangkap)
            if msg.startswith("/") and "\n" not in msg and not msg.startswith("//"):
                prompt_buf = ""
                do_slash(msg)
                continue
            prompt_buf = ""
            send_prompt(msg)
            if state._key_changed:
                state._key_changed = False
                k = state._last_key or ""
                if k and k != "public":
                    set_flash("API key dibaca otomatis ✓")
                else:
                    set_flash("API key: fallback publik (gratis)")
            continue
        if ch in (_KEY_ALT_ENTER, _KEY_SHIFT_ENTER):
            # baris baru lembut (tidak mengirim pesan)
            if len(prompt_buf) < MAX_INPUT:
                prompt_buf += "\n"
            continue
        if ch in (curses.KEY_BACKSPACE, 127, 8):
            prompt_buf = prompt_buf[:-1]
            continue
        if 32 <= ch <= 126 and len(prompt_buf) < MAX_INPUT:
            prompt_buf += chr(ch)
            continue

def settings_screen(stdscr, state):
    fields = [
        ("temperature", f"Temperature (kreativitas) [0.0-2.0]: {state.temperature}"),
        ("max_tokens", f"Max tokens: {state.max_tokens}"),
        ("reasoning", f"Tampilkan reasoning: {'ya' if state.show_reasoning else 'tidak'}"),
        ("agent", f"Agent mode (tools): {'ya' if state.agent_mode else 'tidak'}"),
        ("auto_allow", f"Auto-allow tools: {'ya' if state.auto_allow else 'tidak'}"),
        ("api_key", f"API key override: {state.api_key or '(default)'}"),
    ]
    sel = 0
    while True:
        ch, sel = menu_list(stdscr, " Pengaturan ",
                       [(f[1], "") for f in fields], sel,
                       "", " ↑↓ pilih • Enter: ubah • ESC: kembali ")
        if ch in (27,):
            return
        if ch in (curses.KEY_UP, ord("k")):
            sel = (sel - 1) % len(fields)
        elif ch in (curses.KEY_DOWN, ord("j")):
            sel = (sel + 1) % len(fields)
        elif ch in (10, 13):
            key = fields[sel][0]
            if key in ("reasoning", "agent", "auto_allow"):
                setattr(state, key, not getattr(state, key))
                label = {"reasoning": "Tampilkan reasoning",
                         "agent": "Agent mode (tools)",
                         "auto_allow": "Auto-allow tools"}[key]
                fields[sel] = (key, f"{label}: "
                               + ("ya" if getattr(state, key) else "tidak"))
            elif key == "api_key":
                val = input_line(stdscr, "API key (kosongkan = default):",
                                 state.api_key or "")
                if val and val != "RESIZE":
                    state.api_key = val or None
                    fields[sel] = (key, f"API key override: {state.api_key or '(default)'}")
            else:
                val = input_line(stdscr, f"{fields[sel][1]}:",
                                 str(getattr(state, key)))
                if val and val != "RESIZE":
                    try:
                        if key == "temperature":
                            state.temperature = max(0.0, min(2.0, float(val)))
                        elif key == "max_tokens":
                            state.max_tokens = max(256, min(128000, int(val)))
                        fields[sel] = (key, f"{fields[sel][1].split(':')[0]}: {getattr(state, key)}")
                    except ValueError:
                        pass
        elif ch == curses.KEY_RESIZE:
            pass


def history_screen(stdscr, state):
    def rebuild():
        files = (sorted(SESSION_DIR.glob("*.json"), reverse=True)
                 if SESSION_DIR.exists() else [])
        items = []
        for f in files:
            try:
                data = json.loads(f.read_text(encoding="utf-8"))
                title = data.get("title", f.name)[:40]
                items.append((str(f), f"{title}  ({_model_label(data.get('model', '?'))})"))
            except (OSError, json.JSONDecodeError):
                continue
        if not items:
            items = [("", "— belum ada sesi tersimpan —")]
        return items

    items = rebuild()
    sel = 0
    while True:
        ch, sel = menu_list(stdscr, " Riwayat Sesi ", items, sel, "",
                            " Enter: buka • d: hapus • D: hapus semua • ESC: kembali ",
                            matrix=True)
        if ch in (27,):
            return
        if ch in (curses.KEY_UP, ord("k")):
            sel = (sel - 1) % len(items)
        elif ch in (curses.KEY_DOWN, ord("j")):
            sel = (sel + 1) % len(items)
        elif ch in (10, 13):
            path, _ = items[sel]
            if path:
                # muat sesi LALU langsung buka chat-nya (sebelumnya
                # cuma balik ke menu utama — sesi tampak "tidak kebuka")
                state.load_session(path)
                chat_screen(stdscr, state)
                return
        elif ch in (ord("d"),):
            path, _ = items[sel]
            if path and confirm(stdscr, f"Hapus sesi ini? {Path(path).name}"):
                try:
                    Path(path).unlink()
                    items = rebuild()
                    sel = 0
                except OSError:
                    pass
        elif ch in (ord("D"), ord("a"), ord("A")):
            n = (sum(1 for _ in SESSION_DIR.glob("*.json"))
                 if SESSION_DIR.exists() else 0)
            if n == 0:
                continue
            if confirm(stdscr, f"Hapus SEMUA sesi ({n} file)? Tindakan ini tidak bisa dibatalkan."):
                try:
                    for p in SESSION_DIR.glob("*.json"):
                        p.unlink()
                    items = rebuild()
                    sel = 0
                except OSError:
                    pass
        elif ch == curses.KEY_RESIZE:
            pass


def _key_status(state):
    """Status API key untuk dashboard statistik (tanpa membocorkan isinya)."""
    k = resolve_key(state.direct, state.api_key)
    if k is None:
        return "tidak tersedia (mode direct)"
    if k == "public":
        return "publik (gratis) — tanpa key"
    return "aktif (auto-detect)"


_WALKER_FRAMES = (
    (" (o.o) ", "  /|\\  ", "  / \\  "),   # langkah lebar
    (" (o.o) ", "  /|\\  ", "  \\_/  "),   # kaki rapat
)


def stats_screen(stdscr, state):
    """Dashboard statistik ala 'walk' Termux: grafik batang animasi,
    walker ASCII yang hop di sepanjang tanah, dan jam WIB live."""
    def compute():
        n_files = (sum(1 for _ in SESSION_DIR.glob("*.json"))
                   if SESSION_DIR.exists() else 0)
        msgs = state.messages
        n_msgs = len(msgs)
        total_chars = sum(len(m.get("content") or "")
                          for m in msgs)
        n_tools = sum(1 for m in msgs
                      if m.get("tool_calls") or m.get("role") == "tool")
        n_sessions_chars = 0
        try:
            for f in SESSION_DIR.glob("*.json"):
                n_sessions_chars += f.stat().st_size
        except OSError:
            pass
        bars = [
            ("Pesan aktif", n_msgs),
            ("Konteks", total_chars),
            ("Tool calls", n_tools),
            ("Sesi di disk", n_files),
            ("Isi disk (byte)", n_sessions_chars),
        ]
        info = [
            ("Model", _model_label(state.model)),
            ("Folder kerja", str(state.cwd)),
            ("Agent mode", "ON" if state.agent_mode else "OFF"),
            ("Auto-allow", "ON" if state.auto_allow else "OFF"),
            ("Reasoning", "ya" if state.show_reasoning else "tidak"),
            ("API key", _key_status(state)),
        ]
        return bars, info

    bars, info = compute()
    phase = 0
    max_val = max((v for _, v in bars), default=1) or 1
    while True:
        h, w = stdscr.getmaxyx()
        stdscr.erase()
        if h < 12 or w < 34:
            try:
                stdscr.addstr(max(0, h // 2), max(0, (w - 22) // 2),
                              "Layar terlalu kecil", curses.A_DIM)
            except curses.error:
                pass
            stdscr.noutrefresh()
            curses.doupdate()
            ch = _next_key(0.2)
            if ch in (10, 13, 27, ord("q"), ord("Q")):
                return
            continue

        box_w = min(w - 2, 66)
        box_h = min(h - 2, 22)
        bx = max(0, (w - box_w) // 2)
        by = max(0, (h - box_h) // 2)
        cy = by + 1
        cx = bx + 1
        inner_w = box_w - 2

        def put(y, x, text, attr=0):
            avail = inner_w - max(0, x - cx)
            if avail < 1:
                return
            try:
                stdscr.addnstr(y, x, text, avail, attr)
            except curses.error:
                pass

        def putch(y, x, ch, attr=0):
            try:
                stdscr.addch(y, x, ch, attr)
            except curses.error:
                pass

        # frame
        try:
            stdscr.addstr(by, bx, "┌" + "─" * (inner_w - 2) + "┐",
                          curses.color_pair(1))
            stdscr.addstr(by + box_h - 1, bx, "└" + "─" * (inner_w - 2) + "┘",
                          curses.color_pair(1))
            for yy in range(by + 1, by + box_h - 1):
                stdscr.addch(yy, bx, "│", curses.color_pair(1))
                stdscr.addch(yy, bx + box_w - 1, "│", curses.color_pair(1))
        except curses.error:
            pass

        y = cy
        # judul + jam live
        put(y, bx + 2, " STATISTIK DENZYX ", curses.A_BOLD | curses.color_pair(1))
        put(y, bx + box_w - 2 - 19, f"{_wib_time():%d-%m-%Y %H:%M:%S} WIB",
            curses.A_BOLD | curses.color_pair(2))
        y += 1
        put(y, bx + 2, f" {APP_NAME} v{APP_VERSION} · dashboard sesi & konteks",
            curses.A_DIM)
        y += 2

        # grafik batang animasi (scanner + shimmer)
        lw = min(max((len(l) for l, _ in bars), default=8), 14)
        bar_w = max(4, inner_w - lw - 24)
        scanner = phase % max(1, bar_w)
        for l, v in bars:
            if y >= by + box_h - 3:
                break
            put(y, cx + 2, f"{l:<{lw}}", curses.A_BOLD)
            fill = round(v / max_val * bar_w)
            for i in range(bar_w):
                if i < fill:
                    a = (curses.A_BOLD if i == scanner else 0) | curses.color_pair(1)
                    putch(y, cx + 2 + lw + 1 + i, "█", a)
                else:
                    putch(y, cx + 2 + lw + 1 + i, "░", curses.A_DIM)
            pct = round(v / max_val * 100) if max_val else 0
            put(y, cx + 2 + lw + 1 + bar_w + 1,
                f"{v:>11,} {pct:>3}%", curses.A_DIM)
            y += 1
        y += 1

        # walker hop di atas tanah
        gy = y + 3
        for i in range(inner_w - 4):
            putch(gy, cx + 2 + i, "─", curses.A_DIM | curses.color_pair(1))
        wstep = max(1, inner_w - 4)
        wx = (phase // 2) % wstep
        bob = 1 if ((phase // 2) % 2) else 0
        top = gy - 3 - bob
        for k in (1, 2, 3):
            px = wx - k
            if px >= 0:
                putch(top + 1, cx + 2 + px, "·", curses.A_DIM | curses.color_pair(2))
        frame = _WALKER_FRAMES[(phase // 2) % 2]
        for i, ln in enumerate(frame):
            put(top + i, cx + 2 + wx, ln, curses.A_BOLD | curses.color_pair(2))
        y = gy + 2

        # info 2 kolom
        per = max(1, (inner_w - 4) // 2)
        ncol = 2 if inner_w >= 46 else 1
        for idx, (l, v) in enumerate(info):
            yy = y + idx // ncol
            if yy >= by + box_h - 2:
                break
            put(yy, cx + 2 + (idx % ncol) * per, f"{l}: {v}")

        # footer
        put(by + box_h - 2, bx + 2, " ↑↓/q/ESC: tutup ", curses.A_DIM)

        stdscr.noutrefresh()
        curses.doupdate()

        ch = _next_key(0.06)
        if ch is None:
            phase += 1
            continue
        if ch in (10, 13, 27, ord("q"), ord("Q")):
            return
        if ch in (_PASTE_START, _PASTE_END):
            continue
        phase += 1


ABOUT_LINES = [
    f"{APP_NAME} v{APP_VERSION}",
    "",
    "AI agent coding TUI untuk Termux — gratis, tanpa server.",
    "Tools: bash, read, write, edit, glob, grep (dari dscli).",
    "Tanpa API key otomatis pakai free tier publik.",
    "",
    "── Changelog ──",
    "v2.0  Rebranding penuh → DENZYX.",
    "      Tema baru biru/cyan, banner DENZYX, label AI DENZYX.",
    "      Fitur baru: dashboard statistik + layar Tentang.",
    "v1.x  Rilis sebelumnya.",
]


def about_screen(stdscr):
    """Layar Tentang: versi, deskripsi, dan changelog denzyx AI."""
    dialog_note(stdscr, f" {APP_NAME} ", ABOUT_LINES,
                footer="Enter/ESC: tutup")


def main_menu(stdscr, state):
    items = [
        ("💬  Chat Baru", "mulai percakapan"),
        ("📞  Voice Chat", "panggilan suara (dengar & bicara)"),
        ("📂  Riwayat Sesi", "buka sesi tersimpan"),
        ("📊  Statistik", "dashboard sesi & konteks"),
        ("⚙️   Pengaturan", "temperature, key, dll"),
        ("ℹ️   Tentang denzyx AI", "versi & changelog"),
        ("🐛  Lapor Bug", "kirim laporan ke Telegram"),
        ("🚪  Keluar", "simpan & tutup"),
    ]
    sel = 0
    while True:
        _h, _w = stdscr.getmaxyx()
        if _w >= 82 and _h >= 14:
            banner = BANNERS["standard"]
        elif _w >= 62 and _h >= 13:
            banner = BANNERS["small"]
        else:
            banner = None
        ch, sel = menu_list(stdscr, f" {APP_NAME} ", items, sel,
                            f" folder: {state.cwd} ",
                            " ↑↓ pilih • Enter: buka • q: keluar ",
                            banner=banner, matrix=True)
        if ch in (ord("q"), ord("Q")):
            state.save_session()
            return False
        if ch in (curses.KEY_UP, ord("k")):
            sel = (sel - 1) % len(items)
        elif ch in (curses.KEY_DOWN, ord("j")):
            sel = (sel + 1) % len(items)
        elif ch in (10, 13):
            if sel == 0:
                state.messages = []
                state.session_title = ""
                state.saved_id = None
                chat_screen(stdscr, state)
            elif sel == 1:
                # keluar dulu dari curses, jalankan voice call, lalu balik
                curses.endwin()
                try:
                    _voice = Path(__file__).with_name("voice-denz.py")
                    subprocess.call([sys.executable, str(_voice)])
                except Exception as e:  # noqa: BLE001
                    input_line(stdscr, f" Voice chat: {e} (Enter) ")
                stdscr.refresh()
            elif sel == 2:
                history_screen(stdscr, state)
            elif sel == 3:
                stats_screen(stdscr, state)
            elif sel == 4:
                settings_screen(stdscr, state)
            elif sel == 5:
                about_screen(stdscr)
            elif sel == 6:
                ok, err = open_url(BUG_URL)
                if ok:
                    input_line(stdscr,
                               f" Membuka Telegram — {BUG_LABEL}"
                               " (Enter: kembali) ")
                else:
                    input_line(stdscr,
                               f" Telegram: {BUG_LABEL} — {err}"
                               " (Enter) ")
            elif sel == 7:
                state.save_session()
                return False
        elif ch == curses.KEY_RESIZE:
            pass


def main(stdscr):
    global FORE, BACK
    curses.curs_set(0)
    curses.start_color()
    curses.use_default_colors()
    # SIGWINCH: zoom/unzoom HP, rotasi, resize — Termux tidak selalu
    # mengirim laporan \x1b[8;H;Wt, jadi debounce stray-key butuh sinyal.
    # Handler lama (ncurses) dirantai supaya ukuran internal tetap valid.
    try:
        _on_sigwinch._old = signal.getsignal(signal.SIGWINCH)
        signal.signal(signal.SIGWINCH, _on_sigwinch)
    except (AttributeError, ValueError, OSError):  # noqa: BLE001
        pass
    FORE, BACK = -1, -1
    # tema: default merah tua + hitam AMOLED, override via file theme.md
    th = load_theme()
    if curses.COLORS < 256:
        # layar 8-warna: semua nilai >15 dipetakan ke merah agar aman
        th = {k: (v[0], v[1]) for k, v in th.items()}
        for k, v in th.items():
            if v[0] > 15:
                th[k] = (curses.COLOR_RED, v[1])
    curses.init_pair(1, th["banner"][0], th["banner"][1])       # highlight/banner/judul/footer
    curses.init_pair(2, th["user"][0], th["user"][1])           # user
    curses.init_pair(3, th["assistant"][0], th["assistant"][1]) # assistant
    curses.init_pair(4, th["yellow"][0], th["yellow"][1])       # model list
    curses.init_pair(5, th["error"][0], th["error"][1])         # error
    curses.init_pair(6, th["label_user"][0], th["label_user"][1])    # label KAMU
    curses.init_pair(7, th["label_ai"][0], th["label_ai"][1])       # label AI DENZYX
    curses.init_pair(8, th["label_tool"][0], th["label_tool"][1])   # label TOOL
    curses.init_pair(9, th["label_error"][0], th["label_error"][1]) # label ERROR
    curses.init_pair(10, th["md_heading"][0], th["md_heading"][1])  # heading markdown
    curses.init_pair(11, th["md_code"][0], th["md_code"][1])        # kode markdown
    curses.init_pair(12, th["md_bullet"][0], th["md_bullet"][1])    # bullet markdown
    curses.init_pair(13, th["md_quote"][0], th["md_quote"][1])      # kutipan markdown
    try:
        curses.mousemask(curses.ALL_MOUSE_EVENTS | curses.REPORT_MOUSE_POSITION)
    except curses.error:
        pass
    stdscr.keypad(False)  # input diparsing manual via _next_key
    try:
        # PENTING: jangan biarkan ncurses membaca input pending sebelum
        # refresh (typeahead) — dia makan \x1b dari paste & menyimpannya
        # di buffer internal yang tak pernah dibaca.
        curses.typeahead(-1)
    except Exception:  # noqa: BLE001
        pass
    # responsif: ESC dikembalikan cepat (dipakai deteksi bracketed paste)
    try:
        curses.set_escdelay(25)
    except Exception:  # noqa: BLE001
        pass

    _input_stop.clear()
    _input_q = queue.Queue()
    _ensure_reader()

    state = State()
    try:
        while main_menu(stdscr, state):
            pass
    except KeyboardInterrupt:
        try:
            state.save_session()
        except Exception:  # noqa: BLE001
            pass
    except curses.error:
        import traceback as _tb
        try:
            CRASH_LOG.write_text(_tb.format_exc(), encoding="utf-8")
        except Exception:  # noqa: BLE001
            pass
        pass
    except Exception:  # noqa: BLE001 — jaring SEMUA crash: simpan traceback
        import traceback
        tb = traceback.format_exc()
        try:
            CRASH_LOG.write_text(tb, encoding="utf-8")
        except Exception:  # noqa: BLE001
            pass
        try:
            state.save_session()
        except Exception:  # noqa: BLE001
            pass
        try:
            stdscr.clear()
            h, w = stdscr.getmaxyx()
            lines = tb.strip().splitlines()
            for i, ln in enumerate(lines[: max(1, h - 4)]):
                stdscr.addnstr(2 + i, 2, ln[: w - 4], w - 4)
            stdscr.addnstr(max(1, h - 2), 2,
                           f"Crash log: {CRASH_LOG} — tekan tombol apa pun untuk keluar",
                           max(1, w - 4))
            stdscr.refresh()
            _next_key()
        except Exception:  # noqa: BLE001
            pass
    return state


if __name__ == "__main__":
    _cli_args = sys.argv[1:]
    if any(a in ("-h", "--help") for a in _cli_args):
        print(_CLI_HELP)
        sys.exit(0)
    if "--voice" in _cli_args or "voice" in _cli_args:
        _rest = [a for a in _cli_args if a not in ("--voice", "voice")]
        _voice = Path(__file__).with_name("voice-denz.py")
        sys.exit(subprocess.call([sys.executable, str(_voice)] + _rest))
    # aktifkan bracketed paste (terminal membungkus paste dgn \x1b[200~...\x1b[201~)
    try:
        os.write(1, b"\x1b[?2004h")
    except OSError:
        pass
    try:
        st = curses.wrapper(main)
    finally:
        try:
            os.write(1, b"\x1b[?2004l")
        except OSError:
            pass
    print(f"\n👋 Sampai jumpa! Sesi terakhir tersimpan di "
          f"{SESSION_DIR}")
