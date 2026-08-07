#!/usr/bin/env python3
"""
dscli — perpustakaan tool-calling untuk denzyx AI (bukan CLI mandiri).

Modul ini hanya menyediakan definisi tool yang dipakai app utama:
    TOOLS       — schema OpenAI function calling (untuk dikirim ke API)
    TOOL_IMPL   — implementasi fungsi tool (bash, read, write, edit, glob, grep)
    SAFE_TOOLS  — set tool baca-only yang dianggap aman

App utama (denzyx.py) import ketiga simbol ini via `import dscli`.
"""

import glob
import os
import re
import shlex
import shutil
import subprocess
import urllib.parse
import urllib.request
import zipfile
from html.parser import HTMLParser
from pathlib import Path

TOOL_TIMEOUT = 60            # detik per perintah bash
TOOL_BUILD_TIMEOUT = 300     # detik per perintah build/test (lebih lama)
TOOL_DEBUG_TIMEOUT = 60      # detik per sesi debug
WEB_TIMEOUT = 30            # detik per permintaan web
WEB_UA = ("Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
          "(KHTML, like Gecko) Chrome/126.0 Safari/537.36")


# ---------------------------------------------------------------------------
# Tool definitions (OpenAI function calling schema)
# ---------------------------------------------------------------------------
TOOLS = [
    {
        "type": "function",
        "function": {
            "name": "bash",
            "description": "Jalankan perintah shell (bash, Linux). Berguna untuk "
                           "menjalankan python3, ls, cat, mkdir, git, dan lain-lain. "
                           "Output stdout+stderr dikembalikan beserta exit code. "
                           "Gunakan untuk mengerjakan tugas yang butuh eksekusi nyata.",
            "parameters": {
                "type": "object",
                "properties": {
                    "command": {"type": "string",
                                "description": "perintah shell lengkap"},
                    "workdir": {"type": "string",
                                "description": "direktori kerja (opsional)"},
                },
                "required": ["command"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "read",
            "description": "Baca isi file teks. Mengembalikan isi file "
                           "(dipotong jika terlalu besar).",
            "parameters": {
                "type": "object",
                "properties": {
                    "path": {"type": "string", "description": "path file"},
                    "offset": {"type": "integer",
                               "description": "mulai baca dari baris ke-N (opsional)"},
                    "limit": {"type": "integer",
                              "description": "maks baris yang dibaca (opsional)"},
                },
                "required": ["path"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "write",
            "description": "Tulis file teks (membuat baru atau menimpa seluruh isi). "
                           "Direktori induk dibuat otomatis.",
            "parameters": {
                "type": "object",
                "properties": {
                    "path": {"type": "string", "description": "path file tujuan"},
                    "content": {"type": "string", "description": "isi file"},
                },
                "required": ["path", "content"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "edit",
            "description": "Ganti satu kemunculan teks lama dengan teks baru "
                           "di dalam file. Gagal jika teks lama tidak ditemukan.",
            "parameters": {
                "type": "object",
                "properties": {
                    "path": {"type": "string", "description": "path file"},
                    "old": {"type": "string", "description": "teks lama persis"},
                    "new": {"type": "string", "description": "teks pengganti"},
                },
                "required": ["path", "old", "new"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "glob",
            "description": "Cari file berdasarkan pola nama, misal '**/*.py'. "
                           "Mengembalikan daftar path.",
            "parameters": {
                "type": "object",
                "properties": {
                    "pattern": {"type": "string", "description": "pola glob"},
                    "path": {"type": "string",
                             "description": "direktori awal (opsional, default cwd)"},
                },
                "required": ["pattern"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "grep",
            "description": "Cari teks/pola regex di dalam file-file. "
                           "Mengembalikan baris yang cocok beserta path dan nomor baris.",
            "parameters": {
                "type": "object",
                "properties": {
                    "pattern": {"type": "string", "description": "pola regex"},
                    "path": {"type": "string",
                             "description": "direktori/file awal (opsional, default cwd)"},
                    "include": {"type": "string",
                                "description": "filter nama file, misal '*.py' (opsional)"},
                },
                "required": ["pattern"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "websearch",
            "description": "Cari web secara realtime (sama seperti tool websearch "
                           "di opencode). Mengembalikan daftar hasil: judul, URL, "
                           "cuplikan. Gunakan untuk info terbaru / di luar "
                           "pengetahuan model.",
            "parameters": {
                "type": "object",
                "properties": {
                    "query": {"type": "string",
                              "description": "kata kunci pencarian"},
                    "num_results": {"type": "integer",
                                    "description": "jumlah hasil (opsional, "
                                                   "default 8, maks 20)"},
                },
                "required": ["query"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "webfetch",
            "description": "Ambil isi halaman web dari URL dan ubah ke teks "
                           "mirip markdown (sama seperti tool webfetch di "
                           "opencode). Pakai untuk membaca halaman hasil "
                           "websearch.",
            "parameters": {
                "type": "object",
                "properties": {
                    "url": {"type": "string", "description": "URL lengkap http(s)"},
                    "format": {"type": "string",
                               "description": "format output: 'markdown' atau "
                                              "'text' (opsional, default markdown)"},
                    "timeout": {"type": "integer",
                                "description": "batas waktu detik (opsional)"},
                },
                "required": ["url"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "task",
            "description": "Delegasikan pekerjaan kompleks / multi-langkah ke "
                           "sub-agent yang berjalan otonom dengan konteks baru "
                           "(sama seperti tool task di opencode). Sub-agent "
                           "memiliki tool sendiri (bash, read, write, edit, "
                           "glob, grep, websearch, webfetch, task) dan "
                           "menjalankannya tanpa konfirmasi. Hasil akhir "
                           "dikembalikan sebagai output tool ini. Cocok untuk "
                           "riset terpisah atau bagian yang bisa dikerjakan "
                           "independen.",
            "parameters": {
                "type": "object",
                "properties": {
                    "prompt": {"type": "string",
                               "description": "instruksi detail untuk sub-agent"},
                    "description": {"type": "string",
                                    "description": "deskripsi singkat tugas "
                                                   "(opsional)"},
                },
                "required": ["prompt"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "device",
            "description": "Akses perangkat Android via Termux-API: battery, "
                           "clipboard, notifikasi, torch, brightness, volume, "
                           "vibrate, toast, TTS, lokasi, SMS, log panggilan, "
                           "kontak, wifi, sensor, info perangkat, kamera, "
                           "media scan, wallpaper, buka URL, setup storage. "
                           "action = nama fitur; args = parameter opsional. "
                           "Contoh: device(action='battery'); "
                           "device(action='notify', args={'title': 'Halo', "
                           "'content': 'Denz'}); device(action='sms_send', "
                           "args={'number': '08xx', 'text': 'teks'}). "
                           "Beberapa fitur butuh izin Android yang diberikan "
                           "sekali lewat dialog layar.",
            "parameters": {
                "type": "object",
                "properties": {
                    "action": {"type": "string",
                               "description": "nama fitur perangkat"},
                    "args": {"type": "object",
                             "description": "parameter fitur (opsional)"},
                },
                "required": ["action"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "tree",
            "description": "Tampilkan struktur folder seperti perintah 'tree' "
                           "(folder besar dijadiin ringkas). Berguna untuk "
                           "memahami struktur proyek yang gede sebelum "
                           "ngoding/debugging.",
            "parameters": {
                "type": "object",
                "properties": {
                    "path": {"type": "string",
                             "description": "direktori (opsional, default cwd)"},
                    "depth": {"type": "integer",
                              "description": "kedalaman maksimal (opsional, "
                                             "default 3)"},
                    "show_hidden": {"type": "boolean",
                                    "description": "tampilkan folder tersembunyi "
                                                   "(opsional, default false)"},
                    "pattern": {"type": "string",
                                "description": "hanya tampilkan file/folder yang "
                                               "cocok pola regex nama (opsional)"},
                },
                "required": [],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "build",
            "description": "Build / test / run / bersihkan / install dependency "
                           "untuk proyek. Deteksi otomatis tipe proyek "
                           "(Python, Node.js, Go, Rust, C/C++ CMake/Make, "
                           "Gradle, Maven). target: build, test, run, deps, "
                           "clean. Gunakan saat user minta 'build aplikasinya', "
                           "'jalanin', 'jalankan test', dsb.",
            "parameters": {
                "type": "object",
                "properties": {
                    "workdir": {"type": "string",
                                "description": "direktori proyek (opsional, "
                                               "default cwd)"},
                    "target": {"type": "string",
                               "description": "build | test | run | deps | clean "
                                              "(default build)"},
                    "args": {"type": "array",
                             "items": {"type": "string"},
                             "description": "argumen tambahan (opsional)"},
                },
                "required": [],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "debug",
            "description": "Debugging. Dua mode:\n"
                           "1) mode 'analyze': kasih target = file log/teks, "
                           "tool ini mengekstrak error, exception, dan "
                           "stack trace untuk cari akar masalah.\n"
                           "2) default: jalankan target (script/binary) di "
                           "bawah debugger (python -m pdb, gdb, lldb, dlv, "
                           "strace) lalu ambil backtrace. Gunakan saat ada "
                           "crash/bug: 'kok error?', 'debug ini', "
                           "'cek lognya'.",
            "parameters": {
                "type": "object",
                "properties": {
                    "target": {"type": "string",
                               "description": "path file log untuk dianalisa, "
                                              "atau file script/binary yang "
                                              "di-debug (opsional)"},
                    "workdir": {"type": "string",
                                "description": "direktori kerja (opsional, "
                                               "default cwd)"},
                    "args": {"type": "array",
                             "items": {"type": "string"},
                             "description": "argumen program (opsional)"},
                    "mode": {"type": "string",
                             "description": "analyze | auto | pdb | gdb | lldb "
                                            "| dlv | strace (opsional, default "
                                            "auto)"},
                    "timeout": {"type": "integer",
                                "description": "batas waktu detik (opsional)"},
                },
                "required": [],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "logs",
            "description": "Baca / tail / filter file log. Kalau path tidak "
                           "disebut, cari otomatis file *.log terbaru di "
                           "proyek. follow=true memantau baris log baru selama "
                           "beberapa detik (untuk debugging live).",
            "parameters": {
                "type": "object",
                "properties": {
                    "path": {"type": "string",
                             "description": "path file log atau direktori "
                                            "(opsional, default auto-cari)"},
                    "workdir": {"type": "string",
                                "description": "direktori kerja (opsional)"},
                    "lines": {"type": "integer",
                              "description": "jumlah baris terakhir (opsional, "
                                             "default 50)"},
                    "follow": {"type": "boolean",
                               "description": "ikuti baris baru (tail -f) selama "
                                              "3 detik (opsional)"},
                    "pattern": {"type": "string",
                                "description": "regex untuk filter baris "
                                               "(opsional)"},
                },
                "required": [],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "git",
            "description": "Operasi git: status, log, diff, branch, remote, "
                           "add, commit, push, pull, fetch, stash, init, "
                           "clone. action = nama operasi. Untuk add/commit/"
                           "clone/init, kirim argumen lewat 'args'.",
            "parameters": {
                "type": "object",
                "properties": {
                    "action": {"type": "string",
                               "description": "status | log | diff | branch | "
                                              "remote | add | commit | push | "
                                              "pull | fetch | stash | init | "
                                              "clone (default status)"},
                    "args": {"type": "array",
                             "items": {"type": "string"},
                             "description": "argumen operasi, mis. ['file.py'] "
                                            "untuk add, ['pesan commit'] untuk "
                                            "commit (opsional)"},
                    "workdir": {"type": "string",
                                "description": "direktori repo (opsional, "
                                               "default cwd)"},
                },
                "required": [],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "pkg",
            "description": "Manajemen package/dependency: check, install, "
                           "list, search. Deteksi otomatis package manager "
                           "(npm, pip, go, cargo). Contoh: pkg(action='install', "
                           "name='requests') atau pkg(action='check').",
            "parameters": {
                "type": "object",
                "properties": {
                    "action": {"type": "string",
                               "description": "check | install | list | search "
                                              "(default check)"},
                    "name": {"type": "string",
                             "description": "nama package (dipakai untuk "
                                            "install/search/check)"},
                    "workdir": {"type": "string",
                                "description": "direktori proyek (opsional, "
                                               "default cwd)"},
                },
                "required": [],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "scaffold",
            "description": "Buat proyek baru dari template: python, node, go, "
                           "rust, c, cpp, html, empty. Membuat file-file awal "
                           "yang siap diisi/edit.",
            "parameters": {
                "type": "object",
                "properties": {
                    "type": {"type": "string",
                             "description": "python | node | go | rust | c | "
                                            "cpp | html | empty (default "
                                            "python)"},
                    "path": {"type": "string",
                             "description": "direktori tujuan (opsional, "
                                            "default = nama type)"},
                    "name": {"type": "string",
                             "description": "nama proyek (opsional, default = "
                                            "type)"},
                },
                "required": [],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "sdk",
            "description": "Kelola Android SDK buat build aplikasi Android "
                           "(APK). action: check (status SDK + component), "
                           "setup (install otomatis commandline-tools + "
                           "platform-tools + build-tools + platform, set "
                           "ANDROID_HOME, dan bikin local.properties di "
                           "proyek), install (tambah component seperti "
                           "'platforms;android-35' / 'build-tools;35.0.0' / "
                           "'platform-tools'), list (component terinstal), "
                           "adb (jalankan perintah adb, default 'devices'). "
                           "Butuh JDK + Gradle (biasanya sudah ada). "
                           "Jalankan sdk(action='check') dulu untuk lihat "
                           "kondisi.",
            "parameters": {
                "type": "object",
                "properties": {
                    "action": {"type": "string",
                               "description": "check | setup | install | list "
                                              "| adb (default check)"},
                    "args": {"type": "object",
                             "description": "parameter opsional: component, "
                                            "path, build_tools, platform, "
                                            "workdir, command"},
                },
                "required": ["action"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "ssh",
            "description": "Akses mesin lain lewat SSH. action: run (jalankan "
                           "perintah di remote), copy (kirim file lokal ke "
                           "remote via scp), fetch (ambil file dari remote). "
                           "Contoh: ssh(action='run', host='user@192.168.1.5', "
                           "command='uptime').",
            "parameters": {
                "type": "object",
                "properties": {
                    "action": {"type": "string",
                               "description": "run | copy | fetch "
                                              "(default run)"},
                    "host": {"type": "string",
                             "description": "host tujuan: user@alamat_ip "
                                            "(wajib)"},
                    "command": {"type": "string",
                                "description": "perintah shell yang dijalankan "
                                               "di remote (action=run)"},
                    "path": {"type": "string",
                             "description": "file lokal (action=copy) atau "
                                            "tujuan simpan (action=fetch)"},
                    "remote": {"type": "string",
                               "description": "path di remote "
                                              "(action=copy/fetch)"},
                },
                "required": [],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "download",
            "description": "Unduh file dari URL ke disk lokal pakai curl/wget, "
                           "dengan retry dan resume. Contoh: "
                           "download(url='https://...', output='~/file.zip').",
            "parameters": {
                "type": "object",
                "properties": {
                    "url": {"type": "string",
                            "description": "URL file (wajib)"},
                    "output": {"type": "string",
                               "description": "path tujuan (opsional, default "
                                              "nama dari URL)"},
                },
                "required": ["url"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "serve",
            "description": "Server HTTP lokal buat berbagi file dari HP ke "
                           "perangkat lain (PC di wifi yang sama). action: "
                           "start (jalankan di background), stop, status. "
                           "Contoh: serve(action='start', path='.', port=8080).",
            "parameters": {
                "type": "object",
                "properties": {
                    "action": {"type": "string",
                               "description": "start | stop | status "
                                              "(default status)"},
                    "path": {"type": "string",
                             "description": "folder yang dibagikan "
                                            "(default cwd)"},
                    "port": {"type": "integer",
                             "description": "port HTTP (default 8080)"},
                },
                "required": [],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "bg",
            "description": "Jalankan perintah di background (nohup) yang tetap "
                           "hidup walau chat ditutup, lalu kelola: list, tail "
                           "(lihat log), kill. Contoh: bg(action='start', "
                           "name='train', command='python3 train.py'); "
                           "bg(action='tail', name='train').",
            "parameters": {
                "type": "object",
                "properties": {
                    "action": {"type": "string",
                               "description": "start | list | tail | kill "
                                              "(default list)"},
                    "name": {"type": "string",
                             "description": "nama job (start/tail/kill)"},
                    "command": {"type": "string",
                                "description": "perintah shell (action=start)"},
                    "lines": {"type": "integer",
                              "description": "jumlah baris log terakhir "
                                             "(default 30)"},
                },
                "required": [],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "root",
            "description": "Eksekusi perintah dengan akses root (su), kalau "
                           "perangkat di-root. action: check (cek su tersedia), "
                           "exec (jalankan perintah sebagai root). Contoh: "
                           "root(action='exec', command='ls /data').",
            "parameters": {
                "type": "object",
                "properties": {
                    "action": {"type": "string",
                               "description": "check | exec (default check)"},
                    "command": {"type": "string",
                                "description": "perintah yang dijalankan "
                                               "sebagai root (action=exec)"},
                },
                "required": [],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "media",
            "description": "Multimedia: play (putar file audio/video lewat "
                           "termux-media-player), stop, record (rekam suara "
                           "lewat termux-microphone-record), info (detail "
                           "file pakai ffprobe). Contoh: "
                           "media(action='play', file='musik.mp3').",
            "parameters": {
                "type": "object",
                "properties": {
                    "action": {"type": "string",
                               "description": "play | stop | record | info "
                                              "(default info)"},
                    "file": {"type": "string",
                             "description": "path file audio/video "
                                            "(action=play/info)"},
                    "output": {"type": "string",
                               "description": "path rekaman audio "
                                              "(action=record, default "
                                              "~/recording.m4a)"},
                },
                "required": [],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "screenshot",
            "description": "Ambil screenshot layar HP (butuh termux-api). "
                           "Contoh: screenshot(path='~/ss.png', delay=0). "
                           "Path opsional; default di folder Pictures.",
            "parameters": {
                "type": "object",
                "properties": {
                    "path": {"type": "string",
                             "description": "path simpan PNG (opsional)"},
                    "delay": {"type": "integer",
                              "description": "tunda detik sebelum capture "
                                             "(default 0)"},
                },
                "required": [],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "sys",
            "description": "Info sistem sekilas: OS, uptime, CPU, memori, "
                           "disk, alamat IP. Bagus buat cek kondisi sebelum "
                           "kerjaan berat. Contoh: sys() atau "
                           "sys(section='mem').",
            "parameters": {
                "type": "object",
                "properties": {
                    "section": {"type": "string",
                                "description": "all | os | cpu | mem | disk | "
                                               "net (default all)"},
                },
                "required": [],
            },
        },
    },
]


# ---------------------------------------------------------------------------
# Tool implementations
# ---------------------------------------------------------------------------

def tool_bash(command, workdir=None):
    try:
        proc = subprocess.run(
            command, shell=True, cwd=workdir or None,
            capture_output=True, text=True, timeout=TOOL_TIMEOUT)
        out = (proc.stdout or "") + (proc.stderr or "")
        return f"exit={proc.returncode}\n{out}" if out.strip() else f"exit={proc.returncode} (tanpa output)"
    except subprocess.TimeoutExpired:
        return f"TIMEOUT setelah {TOOL_TIMEOUT} detik"
    except Exception as e:  # noqa: BLE001
        return f"error: {e}"


def tool_read(path, offset=None, limit=None):
    try:
        p = Path(path)
        if not p.exists():
            return f"error: file tidak ada: {path}"
        if p.is_dir():
            entries = sorted(p.iterdir())
            return "DIR: " + "\n".join(str(e) for e in entries[:100])
        lines = p.read_text(encoding="utf-8", errors="replace").splitlines()
        start = (offset or 1) - 1
        end = len(lines) if limit is None else start + limit
        sel = lines[max(0, start):end]
        body = "\n".join(f"{i + 1}:{ln}" for i, ln in enumerate(sel, start=max(0, start) + 1))
        return body or "(file kosong)"
    except Exception as e:  # noqa: BLE001
        return f"error: {e}"


def tool_write(path, content):
    try:
        p = Path(path)
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(content, encoding="utf-8")
        return f"OK: menulis {len(content)} karakter ke {path}"
    except Exception as e:  # noqa: BLE001
        return f"error: {e}"


def tool_edit(path, old, new):
    try:
        p = Path(path)
        text = p.read_text(encoding="utf-8")
        if old not in text:
            return f"error: teks lama tidak ditemukan di {path}"
        p.write_text(text.replace(old, new, 1), encoding="utf-8")
        return f"OK: mengganti 1 kemunculan di {path}"
    except Exception as e:  # noqa: BLE001
        return f"error: {e}"


def tool_glob(pattern, path=None):
    try:
        base = Path(path or ".")
        hits = sorted(str(p) for p in base.glob(pattern))[:200]
        return "\n".join(hits) if hits else f"(tidak ada file cocok: {pattern})"
    except Exception as e:  # noqa: BLE001
        return f"error: {e}"


def tool_grep(pattern, path=None, include=None):
    try:
        base = Path(path or ".")
        rx = re.compile(pattern)
        hits = []
        files = [base] if base.is_file() else list(base.rglob(include or "*"))
        for f in files[:300]:
            if not f.is_file():
                continue
            try:
                for i, ln in enumerate(f.read_text(encoding="utf-8",
                                                   errors="replace").splitlines(), 1):
                    if rx.search(ln):
                        hits.append(f"{f}:{i}: {ln[:200]}")
                        if len(hits) >= 100:
                            break
            except Exception:  # noqa: BLE001
                continue
            if len(hits) >= 100:
                break
        return "\n".join(hits) if hits else "(tidak ada yang cocok)"
    except re.error as e:
        return f"error regex: {e}"
    except Exception as e:  # noqa: BLE001
        return f"error: {e}"


# ---------------------------------------------------------------------------
# Web tools (paruh mirip opencode: websearch + webfetch, keyless)
# ---------------------------------------------------------------------------

class _DDGScraper(HTMLParser):
    """Scraper hasil lite.duckduckgo.com — hasil: {title, url, snippet}."""

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.results = []
        self._anchor = None
        self._in_snippet = False
        self._snippet = ""

    def handle_starttag(self, tag, attrs):
        a = dict(attrs)
        if tag == "a" and a.get("rel") == "nofollow":
            href = a.get("href") or ""
            if "uddg=" in href:
                self._anchor = {"title": "", "url": _uddg_url(href)}
                self._in_snippet = False
                self._snippet = ""
        elif tag in ("td", "div") and "snippet" in (a.get("class") or ""):
            self._in_snippet = True
            self._snippet = ""

    def handle_data(self, data):
        if self._anchor is not None and not self._in_snippet:
            self._anchor["title"] += data
        elif self._in_snippet:
            self._snippet += data

    def handle_endtag(self, tag):
        if tag == "a" and self._anchor is not None:
            t = " ".join(self._anchor["title"].split())
            if t and self._anchor["url"]:
                self.results.append(
                    {"title": t, "url": self._anchor["url"], "snippet": ""})
            self._anchor = None
        elif tag in ("td", "div") and self._in_snippet:
            if self.results:
                self.results[-1]["snippet"] = " ".join(self._snippet.split())
            self._in_snippet = False


def _uddg_url(href):
    """Decode href redirect DDG ('//duckduckgo.com/l/?uddg=…') ke URL asli."""
    if "uddg=" not in href:
        return href
    q = urllib.parse.parse_qs(urllib.parse.urlparse(href).query)
    return q.get("uddg", [""])[0] or href


class _HTMLToMarkdown(HTMLParser):
    """Konversi HTML → teks mirip markdown (blok kode, link, heading)."""

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.out = []
        self.skip = 0
        self.in_pre = False
        self.in_a = False
        self.a_href = ""

    def handle_starttag(self, tag, attrs):
        a = dict(attrs)
        if tag in ("script", "style", "noscript", "svg", "iframe"):
            self.skip += 1
        elif tag == "pre":
            self.in_pre = True
            self.out.append("\n```\n")
        elif tag in ("h1", "h2", "h3", "h4", "h5", "h6"):
            self.out.append(f"\n{'#' * int(tag[1])} ")
        elif tag in ("p", "div", "section", "article", "ul", "ol", "table"):
            self.out.append("\n")
        elif tag == "br":
            self.out.append("\n")
        elif tag == "li":
            self.out.append("\n- ")
        elif tag == "blockquote":
            self.out.append("\n> ")
        elif tag in ("td", "th"):
            self.out.append(" | ")
        elif tag == "tr":
            self.out.append("\n")
        elif tag == "a":
            self.in_a = True
            self.a_href = a.get("href", "")
        elif tag == "img":
            alt = a.get("alt", "")
            if alt:
                self.out.append(f"[img: {alt}]")

    def handle_data(self, data):
        if self.skip:
            return
        if self.in_pre:
            self.out.append(data)
        elif data.strip():
            self.out.append(" ".join(data.split()))

    def handle_endtag(self, tag):
        if tag in ("script", "style", "noscript", "svg", "iframe") and self.skip:
            self.skip -= 1
        elif tag == "pre":
            self.in_pre = False
            self.out.append("\n```\n")
        elif tag == "a":
            if self.in_a and self.a_href:
                self.out.append(f" ({self.a_href})")
            self.in_a = False
        elif tag in ("h1", "h2", "h3", "h4", "h5", "h6"):
            self.out.append("\n")


def _http_get(url, timeout=WEB_TIMEOUT):
    req = urllib.request.Request(
        url, headers={"User-Agent": WEB_UA,
                      "Accept-Language": "id,en;q=0.8"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        data = r.read(2_000_000)
        ctype = r.headers.get("Content-Type", "")
        cs = "utf-8"
        m = re.search(r"charset=([\w-]+)", ctype, re.I)
        if m:
            cs = m.group(1)
        return data.decode(cs, errors="replace")


def tool_web_search(query, num_results=None):
    try:
        url = ("https://lite.duckduckgo.com/lite/?"
               + urllib.parse.urlencode({"q": query}))
        raw = _http_get(url, WEB_TIMEOUT)
        p = _DDGScraper()
        p.feed(raw)
        n = max(1, min(int(num_results or 8), 20))
        res = []
        seen = set()
        for r in p.results:
            if r["url"] in seen:
                continue
            seen.add(r["url"])
            res.append(r)
            if len(res) >= n:
                break
        if not res:
            return "(tidak ada hasil pencarian)"
        return "\n\n".join(
            f"{i}. {r['title']}\n   {r['url']}\n   {r['snippet'][:220]}"
            for i, r in enumerate(res, 1))
    except Exception as e:  # noqa: BLE001
        return f"error: {e}"


def tool_web_fetch(url, format="markdown", timeout=None):
    try:
        if not url.startswith(("http://", "https://")):
            return "error: URL harus http:// atau https://"
        raw = _http_get(url, min(int(timeout or WEB_TIMEOUT), 60))
        p = _HTMLToMarkdown()
        p.feed(raw)
        md = "".join(p.out)
        md = re.sub(r"\n{3,}", "\n\n", md).strip()
        if format == "text":
            md = re.sub(r"[#`>\-|]", "", md)
        if len(md) > 6000:
            md = md[:6000] + f"\n[terpotong: total {len(md)} karakter]"
        return md or "(halaman kosong / tidak ada teks)"
    except Exception as e:  # noqa: BLE001
        return f"error: {e}"


# ---------------------------------------------------------------------------
# Device tools (Termux-API: akses langsung ke perangkat Android)
# ---------------------------------------------------------------------------

_DEVICE_TIMEOUT = 20   # detik per perintah device

# action → (argv dasar, {param: flag}). flag None = argumen posisional
# (kecuali clipboard_set 'text' yang lewat stdin). param tidak diisi dilewati.
_DEVICE_TABLE = {
    "battery":             (["termux-battery-status"], {}),
    "clipboard_get":       (["termux-clipboard-get"], {}),
    "clipboard_set":       (["termux-clipboard-set"], {"text": None}),
    "notify":              (["termux-notification"],
                            {"title": "-t", "content": "-c", "id": "-i"}),
    "notification_list":   (["termux-notification-list"], {}),
    "notification_remove": (["termux-notification-remove"], {"id": None}),
    "torch":               (["termux-torch"], {"value": None}),
    "brightness":          (["termux-brightness"], {"value": None}),
    "volume":              (["termux-volume"], {"stream": None, "volume": None}),
    "vibrate":             (["termux-vibrate"], {"duration": "-d"}),
    "toast":               (["termux-toast"], {"text": None}),
    "tts":                 (["termux-tts-speak"], {"text": None}),
    "location":            (["termux-location", "-r", "once"], {}),
    "sms_list":            (["termux-sms-list"], {"limit": "-l"}),
    "sms_inbox":           (["termux-sms-inbox"], {"limit": "-l"}),
    "sms_send":            (["termux-sms-send"], {"number": "-n", "text": "-t"}),
    "call_log":            (["termux-call-log"], {"limit": "-l"}),
    "contacts":            (["termux-contact-list"], {}),
    "wifi_scan":           (["termux-wifi-scaninfo"], {}),
    "wifi_info":           (["termux-wifi-connectioninfo"], {}),
    "sensor":              (["termux-sensor"],
                            {"type": "-s", "limit": "-n", "delay": "-d"}),
    "deviceinfo":          (["termux-telephony-deviceinfo"], {}),
    "cellinfo":            (["termux-telephony-cellinfo"], {}),
    "audio":               (["termux-audio-info"], {}),
    "camera_info":         (["termux-camera-info"], {}),
    "camera_photo":        (["termux-camera-photo"], {"path": None, "camera": "-c"}),
    "media_scan":          (["termux-media-scan"], {"path": "-f", "recursive": "-r"}),
    "open_url":            (["termux-open-url"], {"url": None}),
    "open":                (["termux-open"], {"path": None}),
    "wallpaper":           (["termux-wallpaper"], {"path": "-f"}),
    "download":            (["termux-download"],
                            {"url": None, "dir": "-d", "name": "-o"}),
    "storage_setup":       (["termux-setup-storage"], {}),
    "saf_ls":              (["termux-saf-ls"], {"path": None}),
    "wake_lock":           (["termux-wake-lock"], {}),
    "wake_unlock":         (["termux-wake-unlock"], {}),
}


def _run_dev(argv, stdin=None, timeout=_DEVICE_TIMEOUT):
    try:
        proc = subprocess.run(
            argv, input=stdin, capture_output=True, text=True, timeout=timeout)
        out = (proc.stdout or "").strip()
        err = (proc.stderr or "").strip()
        if out:
            return out
        if err:
            return f"exit={proc.returncode} {err}"
        return f"exit={proc.returncode} (tanpa output)"
    except subprocess.TimeoutExpired:
        return (f"TIMEOUT setelah {timeout} detik — mungkin menunggu izin "
                "Android atau interaksi layar")
    except FileNotFoundError:
        return (f"error: {argv[0]} tidak ditemukan — install package "
                "'termux-api' (pkg install termux-api)")
    except Exception as e:  # noqa: BLE001
        return f"error: {e}"


def tool_device(action=None, args=None):
    if not action or action not in _DEVICE_TABLE:
        acts = ", ".join(sorted(_DEVICE_TABLE))
        return (f"action tidak dikenal: {action!r}. action valid: {acts}\n"
                "Contoh: device(action='battery'); "
                "device(action='notify', args={'title': 'X', 'content': 'Y'}).")
    args = args or {}
    argv = list(_DEVICE_TABLE[action][0])
    flags = _DEVICE_TABLE[action][1]
    stdin = None
    for key, flag in flags.items():
        val = args.get(key)
        if val is None:
            continue
        if isinstance(val, bool):
            if val and flag:
                argv.append(flag)
            continue
        sval = str(val)
        if action == "clipboard_set" and key == "text":
            stdin = sval
            continue
        if flag:
            argv += [flag, sval]
        else:
            argv.append(sval)
    return _run_dev(argv, stdin=stdin)


# ---------------------------------------------------------------------------
# Development tools (build aplikasi + debugging + git + logs + scaffold)
# ---------------------------------------------------------------------------

_DEV_IGNORE_DIRS = {
    "__pycache__", ".git", ".hg", ".svn", "node_modules", ".venv", "venv",
    "env", "build", "dist", ".gradle", ".idea", ".vscode", ".cargo", ".npm",
    ".cache", ".local", "target", ".rustup", "coverage", ".pytest_cache",
    ".next", ".nuxt",
}


def _which(cmd):
    return shutil.which(cmd)


def _run_cmd(cmd, workdir, timeout=TOOL_TIMEOUT, max_out=4000):
    try:
        proc = subprocess.run(
            cmd, shell=True, cwd=workdir or None,
            capture_output=True, text=True, timeout=timeout)
        out = (proc.stdout or "") + (proc.stderr or "")
        out = out.strip()
        if len(out) > max_out:
            out = out[:max_out] + f"\n[terpotong: total {len(out)} karakter]"
        return f"exit={proc.returncode}\n{out}" if out else \
            f"exit={proc.returncode} (tanpa output)"
    except subprocess.TimeoutExpired:
        return f"TIMEOUT setelah {timeout} detik"
    except Exception as e:  # noqa: BLE001
        return f"error: {e}"


def _detect_project(workdir):
    """Deteksi tipe proyek + perintah build/run/test/deps."""
    d = Path(workdir or ".")
    names = {p.name for p in d.iterdir()} if d.is_dir() else set()
    has = lambda *ns: any(n in names for n in ns)
    info = {"workdir": str(d), "lang": None, "build": None, "run": None,
            "test": None, "deps": None, "main": None}
    if has("Cargo.toml"):
        info.update(lang="Rust (Cargo)", build="cargo build", run="cargo run",
                    test="cargo test", deps="cargo add <pkg>")
    elif has("go.mod"):
        info.update(lang="Go", build="go build ./...", run="go run .",
                    test="go test ./...", deps="go get <pkg>")
    elif has("package.json"):
        info.update(lang="Node.js", build="npm run build", run="npm start",
                    test="npm test", deps="npm install")
    elif has("pyproject.toml", "setup.py", "requirements.txt"):
        info.update(lang="Python",
                    build=("python -m build" if has("pyproject.toml", "setup.py")
                           else "python3 -m compileall -q ."),
                    run="python <file>.py", test="python -m pytest",
                    deps="pip install -r requirements.txt")
    elif has("CMakeLists.txt"):
        info.update(lang="C/C++ (CMake)",
                    build="cmake -S . -B build && cmake --build build",
                    run=None, test="ctest --test-dir build", deps=None)
    elif has("Makefile", "makefile", "GNUmakefile"):
        info.update(lang="C/C++/Generic (Make)", build="make",
                    run=None, test="make test", deps=None)
    elif has("build.gradle", "settings.gradle", "build.gradle.kts"):
        g = "./gradlew" if has("gradlew") else "gradle"
        info.update(lang="Android (Gradle)",
                    build=f"{g} assembleDebug",
                    run=f"{g} installDebug",
                    test=f"{g} test", deps=None)
    elif has("pom.xml"):
        info.update(lang="Java (Maven)", build="mvn -q package",
                    run="mvn -q exec:java", test="mvn -q test", deps=None)
    for cand in ("main.py", "app.py", "main.go", "main.rs", "main.c",
                 "main.cpp", "index.js", "app.js", "server.js", "index.ts"):
        if has(cand):
            info["main"] = cand
    if info["lang"] is None:
        # fallback: deteksi dari ekstensi file di direktori
        files = [p.name for p in d.iterdir() if p.is_file()]
        if any(f.endswith(".py") for f in files):
            info.update(lang="Python", build="python3 -m compileall -q .",
                        run="python <file>.py", test="python -m pytest",
                        deps="pip install <pkg>")
        elif any(f.endswith((".js", ".ts", ".jsx", ".tsx")) for f in files):
            info.update(lang="Node.js", build="npm run build",
                        run="node <file>.js", test="npm test",
                        deps="npm install")
        elif any(f.endswith(".go") for f in files):
            info.update(lang="Go", build="go build ./...", run="go run .",
                        test="go test ./...", deps="go get <pkg>")
        elif any(f.endswith(".rs") for f in files):
            info.update(lang="Rust", build="cargo build", run="cargo run",
                        test="cargo test", deps="cargo add <pkg>")
        elif any(f.endswith((".c", ".h")) for f in files):
            info.update(lang="C", build="make", run=None, test="make test",
                        deps=None)
        elif any(f.endswith((".cpp", ".cc", ".cxx", ".hpp")) for f in files):
            info.update(lang="C++", build="make", run=None, test="make test",
                        deps=None)
    return info


def _pm(base):
    names = {p.name for p in base.iterdir()} if base.is_dir() else set()
    if "package.json" in names:
        return "npm"
    if any(n in names for n in ("pyproject.toml", "setup.py",
                                "requirements.txt")):
        return "pip"
    if "go.mod" in names:
        return "go"
    if "Cargo.toml" in names:
        return "cargo"
    return None


def tool_tree(path=None, depth=3, show_hidden=False, pattern=None):
    base = Path(path or ".")
    if not base.is_dir():
        return f"error: bukan direktori: {base}"
    ignore = _DEV_IGNORE_DIRS
    if show_hidden:
        ignore = ignore - {".git", ".hg", ".svn"}
    rx = re.compile(pattern) if pattern else None
    max_depth = max(1, min(int(depth or 3), 8))
    out = []

    def walk(p, prefix, level):
        if level > max_depth:
            out.append(prefix + "…")
            return
        try:
            entries = sorted(p.iterdir(),
                             key=lambda e: (not e.is_dir(), e.name.lower()))
        except OSError:
            return
        shown = []
        for e in entries:
            if e.is_dir():
                if e.name in ignore:
                    continue
                if rx and not rx.search(e.name):
                    continue
                shown.append(e)
            else:
                if rx and not rx.search(e.name):
                    continue
                shown.append(e)
        for i, e in enumerate(shown):
            last = i == len(shown) - 1
            branch = "└─ " if last else "├─ "
            out.append(prefix + branch + (e.name + "/" if e.is_dir() else e.name))
            if e.is_dir():
                walk(e, prefix + ("   " if last else "│  "), level + 1)

    out.append(base.resolve().name + "/")
    walk(base, "", 1)
    return "\n".join(out) if out else "(folder kosong)"


def tool_build(workdir=None, target="build", args=None):
    target = (target or "build").lower()
    info = _detect_project(workdir)
    cmd = {"build": info["build"], "test": info["test"], "run": info["run"],
           "deps": info["deps"]}.get(target)
    if target == "clean":
        cmd = None
        for cand in ("Makefile", "makefile"):
            if (Path(info["workdir"]) / cand).exists():
                cmd = "make clean"
                break
    if not cmd and target == "run" and info["main"]:
        cmd = f"python3 {info['main']}"
    if cmd and info["main"]:
        cmd = cmd.replace("<file>.py", info["main"])
    if not cmd:
        return (f"target '{target}' tidak tersedia. Deteksi proyek:\n"
                f"- bahasa : {info['lang'] or 'tidak terdeteksi'}\n"
                f"- build  : {info['build'] or '-'}\n"
                f"- run    : {info['run'] or '-'}\n"
                f"- test   : {info['test'] or '-'}\n"
                f"- deps   : {info['deps'] or '-'}")
    if info["lang"] and info["lang"].startswith("Android"):
        sdk = _android_sdk_root()
        if not sdk:
            return (f"target '{target}' butuh Android SDK yang belum ada. "
                    "Jalankan dulu: sdk(action='setup') untuk install "
                    "commandline-tools + platform-tools + build-tools + "
                    "platform otomatis, lalu ulangi build.")
        cmd = f"ANDROID_HOME={sdk} ANDROID_SDK_ROOT={sdk} " + cmd
    if args:
        cmd = f"{cmd} {' '.join(str(a) for a in args)}"
    return f"[{info['lang'] or 'proyek'} | {target}] {cmd}\n" + \
        _run_cmd(cmd, info["workdir"], TOOL_BUILD_TIMEOUT)


def tool_debug(target=None, workdir=None, args=None, mode="auto", timeout=None):
    timeout = max(5, min(int(timeout or TOOL_DEBUG_TIMEOUT), 300))
    base = Path(workdir or ".")
    if mode == "analyze" or (target and str(target).lower().endswith(
            (".log", ".txt", ".err", ".out"))):
        p = Path(target)
        if not p.is_absolute():
            p = base / target
        if not p.is_file():
            return f"error: file log tidak ada: {p}"
        text = p.read_text(encoding="utf-8", errors="replace")
        lines = text.splitlines()
        err_rx = re.compile(
            r"(Traceback|Error|Exception|fatal|panic|Segmentation fault|"
            r"core dumped|FAILED|SyntaxError|NameError|TypeError|"
            r"ValueError|KeyError|IndexError|AttributeError|ImportError|"
            r"Error:|error:)", re.I)
        hits = [l for l in lines if err_rx.search(l)]
        res = [f"file: {p} | baris: {len(lines)} | baris error: {len(hits)}"]
        if hits:
            res.append("— baris error —")
            res.extend(l[:250] for l in hits[:40])
        tb = False
        for i, l in enumerate(lines):
            if l.startswith("Traceback (most recent call last)"):
                tb = True
                start = i
            elif tb and l and not l[0].isspace() and i > start + 1:
                tb = False
            if tb and i > start:
                res.append(f"{i + 1}: {l[:250]}")
        return "\n".join(res) if len(res) > 1 else \
            "(tidak ada indikasi error di log tersebut)"

    if not target:
        info = _detect_project(base)
        target = info["main"]
        if not target:
            return ("error: tentukan target (file script/binary) untuk "
                    "di-debug, atau beri mode='analyze' + target=file log.")
    tpath = base / target
    tpath = tpath if tpath.exists() else Path(target)
    if not tpath.exists():
        return f"error: target tidak ada: {tpath}"
    a = " ".join(str(x) for x in (args or []))
    low = str(target).lower()
    mode = (mode or "auto").lower()
    if mode == "auto":
        if low.endswith(".py"):
            mode = "pdb"
        else:
            mode = "gdb"
    if mode == "pdb":
        cmd = f"python3 -m pdb -c 'continue' -c 'bt' {target} {a}"
    elif mode == "gdb" and _which("gdb"):
        cmd = f"gdb -q -batch -ex run -ex bt --args {target} {a}"
    elif mode == "lldb" and _which("lldb"):
        cmd = f"lldb -b -o run -o bt -- {target} {a}"
    elif mode == "dlv" and _which("dlv"):
        cmd = f"dlv debug {target} -- {a}"
    elif mode == "strace" and _which("strace"):
        cmd = f"strace -f -o /tmp/denz_strace.log {target} {a}"
    else:
        return (f"error: debugger untuk mode '{mode}' tidak tersedia "
                "(install gdb/lldb/dlv).")
    return _run_cmd(cmd, str(base), timeout)


def tool_logs(path=None, workdir=None, lines=50, follow=False, pattern=None):
    base = Path(workdir or ".")
    n = max(1, min(int(lines or 50), 500))
    target = None
    if path:
        p = Path(path)
        if not p.is_absolute():
            p = base / path
        if p.is_dir():
            target = None
        elif p.exists():
            target = p
        else:
            return f"error: log tidak ada: {path}"
    if target is None:
        cands = []
        for f in list(base.rglob("*.log"))[:300]:
            try:
                cands.append((f.stat().st_mtime, f))
            except OSError:
                continue
        for f in list(base.rglob("logs/**/*"))[:100]:
            if f.is_file() and f.suffix in (".log", ".txt", ".out"):
                try:
                    cands.append((f.stat().st_mtime, f))
                except OSError:
                    continue
        cands.sort(key=lambda x: x[0], reverse=True)
        if not cands:
            return "(tidak ada file .log ditemukan di proyek)"
        target = cands[0][1]
    if follow:
        out = _run_cmd(f"timeout 3 tail -f -n {n} '{target}'", str(base), 8)
    else:
        out = _run_cmd(f"tail -n {n} '{target}'", str(base), 15)
    if pattern:
        try:
            rx = re.compile(pattern)
            filtered = [l for l in out.splitlines() if rx.search(l)]
            out = "\n".join(filtered) or "(tidak ada baris cocok pola)"
        except re.error as e:
            out += f"\nerror regex: {e}"
    return f"[log: {target}]\n" + out


_GIT_CMDS = {
    "status": "status -sb", "log": "log --oneline -20",
    "diff": "diff", "diff_cached": "diff --cached",
    "branch": "branch -a", "remote": "remote -v",
    "push": "push", "pull": "pull", "fetch": "fetch --all",
    "stash": "stash list", "init": "init", "clone": "clone",
}


def tool_git(action="status", args=None, workdir=None):
    action = (action or "status").lower()
    base = Path(workdir or ".")
    extra = [str(x) for x in (args or [])]
    if action == "add":
        cmd = f"git add {' '.join(extra) or '.'}"
    elif action == "commit":
        msg = " ".join(extra) or "update"
        cmd = f'git commit -m "{msg}"'
    elif action in ("init", "clone"):
        cmd = f"git {action} {' '.join(extra)}".rstrip()
    elif action in _GIT_CMDS:
        cmd = f"git {_GIT_CMDS[action]}"
    else:
        return ("error: action tidak dikenal. Valid: " +
                ", ".join(sorted(set(_GIT_CMDS) | {"add", "commit"})))
    return _run_cmd(cmd, str(base), TOOL_TIMEOUT)


def tool_pkg(action="check", name=None, workdir=None):
    action = (action or "check").lower()
    base = Path(workdir or ".")
    pm = _pm(base)
    if not pm:
        return ("error: tidak ada package manager terdeteksi "
                "(butuh package.json / pyproject.toml / setup.py / "
                "requirements.txt / go.mod / Cargo.toml).")
    n = name or ""
    table = {
        ("npm", "check"):    "npm ls --depth=0",
        ("npm", "install"):  f"npm install {n}",
        ("npm", "list"):     "npm ls --depth=0",
        ("npm", "search"):   f"npm search {n} --no-description",
        ("npm", "audit"):    "npm audit",
        ("pip", "check"):    (f"pip show {n}" if n else "pip check"),
        ("pip", "install"):  f"pip install {n}",
        ("pip", "list"):     "pip list",
        ("pip", "search"):   f"pip index versions {n}",
        ("go", "check"):     (f"go list -m {n}" if n else "go mod verify"),
        ("go", "install"):   f"go get {n}",
        ("go", "list"):      "go list -m all",
        ("go", "search"):    f"go list -m -versions {n}",
        ("cargo", "check"):  "cargo metadata --no-deps",
        ("cargo", "install"): f"cargo add {n}",
        ("cargo", "list"):   "cargo tree",
        ("cargo", "search"): f"cargo search {n}",
    }
    cmd = table.get((pm, action))
    if not cmd:
        return (f"error: action '{action}' tidak didukung untuk {pm}. "
                "Valid: check, install, list, search.")
    return f"[{pm} {action}]\n" + _run_cmd(cmd, str(base), TOOL_BUILD_TIMEOUT)


_SCAFFOLDS = {
    "python": {
        "main.py": (
            "import sys\n\n\ndef main():\n"
            "    print(\"Hello, Denz!\")\n\n\n"
            'if __name__ == "__main__":\n'
            "    sys.exit(main())\n"),
        "requirements.txt": "# pip install -r requirements.txt\n",
        ".gitignore": ("__pycache__/\n*.pyc\n.venv/\nvenv/\n"
                       "dist/\nbuild/\n"),
    },
    "node": {
        "package.json": (
            '{\n  "name": "{{name}}",\n  "version": "1.0.0",\n'
            '  "main": "index.js",\n  "scripts": {\n'
            '    "start": "node index.js",\n'
            '    "dev": "node --watch index.js"\n  }\n}\n'),
        "index.js": "console.log('Hello, Denz!');\n",
        ".gitignore": "node_modules/\ndist/\n.env\n",
    },
    "go": {
        "go.mod": "module {{name}}\n\ngo 1.22\n",
        "main.go": (
            "package main\n\nimport \"fmt\"\n\n"
            "func main() {\n\tfmt.Println(\"Hello, Denz!\")\n}\n"),
        ".gitignore": "bin/\n",
    },
    "rust": {
        "Cargo.toml": ("[package]\nname = \"{{name}}\"\n"
                       "version = \"0.1.0\"\nedition = \"2021\"\n\n"
                       "[dependencies]\n"),
        "src/main.rs": "fn main() {\n    println!(\"Hello, Denz!\");\n}\n",
        ".gitignore": "target/\n",
    },
    "c": {
        "main.c": (
            '#include <stdio.h>\n\nint main(void) {\n'
            '    printf("Hello, Denz!\\n");\n    return 0;\n}\n'),
        "Makefile": ("CC=gcc\nCFLAGS=-Wall -Wextra -g\n\n"
                     "main: main.c\n\t$(CC) $(CFLAGS) -o $@ $^\n\n"
                     "clean:\n\trm -f main\n"),
        ".gitignore": "main\n",
    },
    "cpp": {
        "main.cpp": (
            "#include <iostream>\n\nint main() {\n"
            '    std::cout << "Hello, Denz!" << std::endl;\n'
            "    return 0;\n}\n"),
        "Makefile": ("CXX=g++\nCXXFLAGS=-Wall -Wextra -g\n\n"
                     "main: main.cpp\n\t$(CXX) $(CXXFLAGS) -o $@ $^\n\n"
                     "clean:\n\trm -f main\n"),
        ".gitignore": "main\n",
    },
    "html": {
        "index.html": (
            "<!DOCTYPE html>\n<html lang=\"id\">\n<head>\n"
            '  <meta charset="UTF-8">\n'
            '  <meta name="viewport" content="width=device-width, '
            'initial-scale=1.0">\n  <title>{{name}}</title>\n'
            '  <link rel="stylesheet" href="style.css">\n</head>\n'
            "<body>\n  <h1>Hello, Denz!</h1>\n"
            '  <script src="script.js"></script>\n</body>\n</html>\n'),
        "style.css": "body { font-family: system-ui, sans-serif; }\n",
        "script.js": "console.log('Hello, Denz!');\n",
    },
    "empty": {
        ".gitignore": "*.log\n.tmp/\n",
    },
}


def tool_scaffold(type="python", path=None, name=None):
    t = (type or "python").lower()
    if t not in _SCAFFOLDS:
        return ("error: tipe tidak dikenal. Valid: " +
                ", ".join(sorted(_SCAFFOLDS)))
    name = (name or t).strip()
    d = Path(path or name)
    try:
        d.mkdir(parents=True, exist_ok=True)
    except OSError as e:
        return f"error: {e}"
    created = []
    for fname, content in _SCAFFOLDS[t].items():
        f = d / fname
        if f.exists():
            continue
        f.parent.mkdir(parents=True, exist_ok=True)
        f.write_text(content.replace("{{name}}", name), encoding="utf-8")
        created.append(fname)
    return (f"OK: scaffold '{t}' dibuat di {d}\nFile: " +
            ", ".join(created or ["(semua sudah ada)"]))


# ---------------------------------------------------------------------------
# Android SDK tools (build aplikasi Android / APK)
# ---------------------------------------------------------------------------

_ANDROID_TOOLS_URL = ("https://dl.google.com/android/repository/"
                      "commandlinetools-linux-11076708_latest.zip")


def _android_sdk_root():
    env = os.environ.get("ANDROID_HOME") or os.environ.get("ANDROID_SDK_ROOT")
    if env:
        return Path(env)
    for cand in (Path.home() / "android-sdk",
                 Path("/data/data/com.termux/files/home/android-sdk"),
                 Path("/data/data/com.termux/files/usr/share/android-sdk")):
        if cand.is_dir():
            return cand
    return None


def _sdkmanager_bin(root):
    if not root:
        return None
    for base in (root / "cmdline-tools" / "latest" / "bin",
                 root / "cmdline-tools" / "bin"):
        b = base / "sdkmanager"
        if b.is_file():
            return b
    return None


def _sdk_installed(root):
    out = {"platform-tools": False, "build-tools": [], "platforms": []}
    if not root or not root.is_dir():
        return out
    out["platform-tools"] = (root / "platform-tools").is_dir()
    bt = (root / "build-tools")
    if bt.is_dir():
        out["build-tools"] = sorted(p.name for p in bt.iterdir())
    pl = (root / "platforms")
    if pl.is_dir():
        out["platforms"] = sorted(p.name for p in pl.iterdir())
    return out


def _install_cmdline_tools(root):
    """Download + ekstrak commandline-tools ke <root>/cmdline-tools/latest."""
    latest = root / "cmdline-tools" / "latest"
    if _sdkmanager_bin(root):
        return None
    (root / "cmdline-tools").mkdir(parents=True, exist_ok=True)
    zip_path = root / "_clt.zip"
    try:
        urllib.request.urlretrieve(_ANDROID_TOOLS_URL, zip_path)
        with zipfile.ZipFile(zip_path) as z:
            z.extractall(root / "cmdline-tools")
        zip_path.unlink(missing_ok=True)
        src = root / "cmdline-tools" / "cmdline-tools"
        if src.is_dir() and not latest.exists():
            src.rename(latest)
    except Exception as e:  # noqa: BLE001
        return f"error download commandline-tools: {e}"
    return None


def _run_sdkmanager(sm, *args, timeout=600):
    try:
        proc = subprocess.run([str(sm)] + list(args),
                              input="y\n" * 40, capture_output=True,
                              text=True, timeout=timeout)
        out = ((proc.stdout or "") + (proc.stderr or "")).strip()
        return f"exit={proc.returncode}\n{out[-1600:]}"
    except subprocess.TimeoutExpired:
        return f"TIMEOUT setelah {timeout} detik"
    except Exception as e:  # noqa: BLE001
        return f"error: {e}"


def tool_sdk(action="check", args=None):
    action = (action or "check").lower()
    args = args or {}
    if action == "check":
        root = _android_sdk_root()
        inst = _sdk_installed(root)
        sm = _sdkmanager_bin(root)
        lines = [
            f"ANDROID_HOME   : {os.environ.get('ANDROID_HOME') or '(tidak diset)'}",
            f"SDK root       : {root or '(belum ada)'}",
            f"platform-tools : {'ada' if inst['platform-tools'] else 'BELUM'}",
            f"build-tools    : {', '.join(inst['build-tools']) or 'BELUM'}",
            f"platforms      : {', '.join(inst['platforms']) or 'BELUM'}",
            f"sdkmanager     : {sm or 'belum (butuh install cmdline-tools)'}",
            f"java           : {_which('java') or 'BELUM'}",
            f"gradle         : {_which('gradle') or 'BELUM'}",
            f"adb            : {_which('adb') or 'belum'}",
        ]
        if sm and inst["platform-tools"] and inst["build-tools"] and inst["platforms"]:
            lines.append("=> SDK lengkap — siap build APK.")
        else:
            lines.append("=> SDK belum lengkap — jalankan sdk(action='setup').")
        return "\n".join(lines)
    if action == "setup":
        target = Path(args.get("path") or (Path.home() / "android-sdk"))
        try:
            target.mkdir(parents=True, exist_ok=True)
        except OSError as e:
            return f"error: {e}"
        os.environ["ANDROID_HOME"] = str(target)
        os.environ["ANDROID_SDK_ROOT"] = str(target)
        err = _install_cmdline_tools(target)
        if err:
            return err
        sm = _sdkmanager_bin(target)
        if not sm:
            return "error: gagal setup commandline-tools."
        _run_sdkmanager(sm, "--licenses", timeout=300)
        comps = ["platform-tools"]
        if args.get("build_tools"):
            comps.append(args["build_tools"])
        if args.get("platform"):
            comps.append(args["platform"])
        res = _run_sdkmanager(sm, *comps, timeout=900)
        out = f"SDK setup di {target} (ANDROID_HOME={target})\n{res}"
        wd = args.get("workdir")
        if wd:
            lp = Path(wd) / "local.properties"
            try:
                lp.write_text(f"sdk.dir={target}\n", encoding="utf-8")
                out += f"\nlocal.properties dibuat: {lp}"
            except OSError as e:
                out += f"\n(local.properties gagal: {e})"
        return out
    if action == "install":
        root = _android_sdk_root()
        comp = args.get("component")
        if not comp:
            return ("error: tentukan component, misal 'platforms;android-35', "
                    "'build-tools;35.0.0', atau 'platform-tools'.")
        if not root:
            return "error: SDK belum ada — jalankan sdk(action='setup') dulu."
        _install_cmdline_tools(root)
        sm = _sdkmanager_bin(root)
        if not sm:
            return "error: gagal setup commandline-tools."
        lic = _run_sdkmanager(sm, "--licenses", timeout=300)
        inst = _run_sdkmanager(sm, comp, timeout=900)
        return f"licenses:\n{lic}\n\ninstall {comp}:\n{inst}"
    if action == "list":
        root = _android_sdk_root()
        sm = _sdkmanager_bin(root) if root else None
        if not sm:
            return "error: sdkmanager belum ada — jalankan sdk(action='setup') dulu."
        return _run_sdkmanager(sm, "--list_installed", timeout=120)
    if action == "adb":
        cmd = ["adb"] + [str(x) for x in (args.get("command") or ["devices"])]
        return _run_cmd(" ".join(cmd), None, 60)
    return ("error: action tidak dikenal. Valid: check, setup, install, "
            "list, adb.")


# ---------------------------------------------------------------------------
# Akses & otomasi: ssh, download, serve, bg, root, media, screenshot, sys
# ---------------------------------------------------------------------------

_RUN_DIR = os.path.expanduser("~/.denzyx/run")


def _run_dir():
    Path(_RUN_DIR).mkdir(parents=True, exist_ok=True)
    return _RUN_DIR


def _lan_urls(port):
    r = _run_cmd("hostname -I 2>/dev/null; ifconfig 2>/dev/null", None, 5)
    ips = []
    for tok in r.split():
        if tok.count(".") == 3 and all(p.isdigit() for p in tok.split(".")):
            first = tok.split(".")[0]
            if first not in ("0", "127", "255") and tok not in ips:
                ips.append(tok)
    if not ips:
        return ("akses dari perangkat lain: cek IP lewat "
                "device(action='wifi_info')")
    return ("akses dari perangkat lain di wifi yang sama:\n" +
            "\n".join(f"  http://{ip}:{port}/" for ip in ips))


def tool_ssh(action="run", host=None, command=None, path=None, remote=None,
             port=None):
    if not host:
        return ("host wajib diisi, contoh: user@192.168.1.5. Cek IP tujuan "
                "lewat device(action='wifi_info').")
    if not (_which("ssh") or _which("scp")):
        return "error: ssh/scp belum terinstall — pkg install openssh"
    action = (action or "run").lower()
    target = host
    if port:
        target = f"-p {int(port)} {host}"
    if action == "run":
        if not command:
            return "action=run butuh parameter 'command'"
        cmd = (f"ssh -o ConnectTimeout=10 {target} "
               f"{shlex.quote(command)}")
    elif action == "copy":
        if not path or not remote:
            return "action=copy butuh 'path' (lokal) dan 'remote'"
        cmd = f"scp {shlex.quote(path)} {host}:{shlex.quote(remote)}"
    elif action == "fetch":
        if not remote or not path:
            return "action=fetch butuh 'remote' dan 'path' (tujuan lokal)"
        cmd = f"scp {host}:{shlex.quote(remote)} {shlex.quote(path)}"
    else:
        return f"action tidak dikenal: {action} (run|copy|fetch)"
    return f"[ssh {host} | {action}] {cmd}\n" + _run_cmd(cmd, None, 120)


def tool_download(url, output=None):
    if not url:
        return "url wajib diisi"
    out = output or os.path.basename(urllib.parse.urlparse(url).path) or "download.bin"
    out = os.path.expanduser(out)
    if _which("curl"):
        cmd = (f"curl -L --fail --retry 3 -C - "
               f"-o {shlex.quote(out)} {shlex.quote(url)}")
    elif _which("wget"):
        cmd = f"wget -c -O {shlex.quote(out)} {shlex.quote(url)}"
    else:
        return "error: curl/wget belum terinstall — pkg install curl"
    r = _run_cmd(cmd, None, 600)
    try:
        r += f"\nukuran: {os.path.getsize(out)} byte"
    except OSError:
        pass
    return r


def tool_serve(action="status", path=None, port=8080):
    run_dir = _run_dir()
    pid_file = os.path.join(run_dir, "serve.pid")
    log_file = os.path.join(run_dir, "serve.log")
    action = (action or "status").lower()
    if action == "start":
        base = os.path.abspath(os.path.expanduser(path or "."))
        if not os.path.isdir(base):
            return f"error: folder tidak ada: {base}"
        if os.path.exists(pid_file):
            pid = open(pid_file).read().strip()
            return (f"server sudah jalan (pid {pid}). Stop dulu kalau mau "
                    "ganti port/folder.")
        cmd = (f"nohup python3 -m http.server {int(port)} --bind 0.0.0.0 "
               f"--directory {shlex.quote(base)} > {log_file} 2>&1 & "
               f"echo $! > {pid_file}")
        r = _run_cmd(cmd, None, 10)
        return (f"server jalan di http://0.0.0.0:{port} (folder {base})\n"
                f"{r}\n{_lan_urls(port)}")
    if action == "stop":
        if not os.path.exists(pid_file):
            return "server tidak jalan."
        pid = open(pid_file).read().strip()
        _run_cmd(f"kill {pid} 2>/dev/null; rm -f {pid_file}", None, 10)
        return f"server dihentikan (pid {pid})."
    if not os.path.exists(pid_file):
        return "server tidak jalan. serve(action='start', path='.', port=8080)"
    pid = open(pid_file).read().strip()
    return f"pid {pid}: {_run_cmd(f'kill -0 {pid} 2>/dev/null && echo hidup || echo mati', None, 5)}\n{_lan_urls(port)}"


def tool_bg(action="list", name=None, command=None, lines=30):
    run_dir = _run_dir()
    bg_dir = os.path.join(run_dir, "bg")
    os.makedirs(bg_dir, exist_ok=True)
    action = (action or "list").lower()
    safe = lambda n: re.sub(r"[^A-Za-z0-9_.-]", "_", n or "job")
    if action == "start":
        if not name or not command:
            return "action=start butuh 'name' dan 'command'"
        pidf = os.path.join(bg_dir, f"{safe(name)}.pid")
        logf = os.path.join(bg_dir, f"{safe(name)}.log")
        if os.path.exists(pidf):
            pid = open(pidf).read().strip()
            if os.path.exists(f"/proc/{pid}"):
                return f"job '{name}' sudah jalan (pid {pid})."
        cmd = (f"nohup bash -c {shlex.quote(command)} > {shlex.quote(logf)} 2>&1 "
               f"& echo $! > {shlex.quote(pidf)}")
        r = _run_cmd(cmd, None, 10)
        try:
            pid = open(pidf).read().strip()
        except OSError:
            pid = "?"
        return f"[bg] '{name}' jalan dengan pid {pid}\n{r}\nlog: {logf}"
    if action == "tail":
        if not name:
            return "action=tail butuh 'name'"
        logf = os.path.join(bg_dir, f"{safe(name)}.log")
        return _run_cmd(f"tail -n {int(lines)} {shlex.quote(logf)} 2>/dev/null "
                        "|| echo '(belum ada log)'", None, 10)
    if action == "kill":
        if not name:
            return "action=kill butuh 'name'"
        pidf = os.path.join(bg_dir, f"{safe(name)}.pid")
        if not os.path.exists(pidf):
            return f"job '{name}' tidak ada."
        pid = open(pidf).read().strip()
        r = _run_cmd(f"kill {pid} 2>/dev/null; rm -f {shlex.quote(pidf)}", None, 10)
        return f"[bg] '{name}' dihentikan (pid {pid}).\n{r}"
    rows = []
    for pidf in sorted(glob.glob(os.path.join(bg_dir, "*.pid"))):
        nm = os.path.basename(pidf)[:-4]
        pid = open(pidf).read().strip()
        status = "HIDUP" if os.path.exists(f"/proc/{pid}") else "mati"
        rows.append(f"{nm:24} pid={pid:>6} {status}")
    return "\n".join(rows) if rows else "(tidak ada job background)"


def tool_root(action="check", command=None):
    action = (action or "check").lower()
    if action == "exec":
        if not command:
            return "action=exec butuh 'command'"
        if os.geteuid() == 0:
            cmd = command
        elif _which("su"):
            cmd = f"su -c {shlex.quote(command)}"
        else:
            return ("error: bukan root dan 'su' tidak tersedia (perangkat "
                    "belum di-root).")
        return f"[root exec] {cmd}\n" + _run_cmd(cmd, None, 60)
    r = _run_cmd("id", None, 5)
    su = f"su: {_which('su') or 'tidak ada'}"
    return f"{r}\n{su}"


def tool_media(action="info", file=None, output=None):
    action = (action or "info").lower()
    if action == "play":
        if not file:
            return "action=play butuh 'file'"
        f = os.path.expanduser(file)
        if not os.path.exists(f):
            return f"error: file tidak ada: {f}"
        if _which("termux-media-player"):
            return _run_dev(["termux-media-player", "play", f], timeout=30)
        if _which("ffplay"):
            return _run_cmd(f"ffplay -nodisp -autoexit {shlex.quote(f)}",
                            None, 30)
        return "error: butuh termux-api atau ffmpeg buat play"
    if action == "stop":
        if _which("termux-media-player"):
            return _run_dev(["termux-media-player", "stop"], timeout=10)
        return _run_cmd("pkill -f ffplay 2>/dev/null; echo ok", None, 10)
    if action == "record":
        out = os.path.expanduser(output or "~/recording.m4a")
        if _which("termux-microphone-record"):
            return _run_cmd(
                f"termux-microphone-record -f {shlex.quote(out)} -l 5 2>&1; "
                f"echo 'rekaman selesai: {shlex.quote(out)}'", None, 30)
        return "error: butuh package termux-api (termux-microphone-record)"
    if not file:
        return "action=info butuh 'file' (atau action play/stop/record)"
    f = os.path.expanduser(file)
    if _which("ffprobe"):
        return _run_cmd(f"ffprobe -v error -show_format -show_streams "
                        f"{shlex.quote(f)}", None, 30)
    return _run_cmd(f"ls -lh {shlex.quote(f)}", None, 10)


def tool_screenshot(path=None, delay=0):
    argv = ["termux-screenshot"]
    if path:
        argv += ["-c", os.path.expanduser(path)]
    if delay:
        argv += ["-d", str(int(delay))]
    r = _run_dev(argv, timeout=30)
    return r or "(screenshot selesai — cek folder Pictures)"


def tool_sys(section="all"):
    section = (section or "all").lower()
    labels = {"os": "OS", "cpu": "CPU", "mem": "MEMORI", "disk": "DISK",
              "net": "JARINGAN"}
    out = []

    def grab(key, cmd):
        if section in ("all", key):
            out.append(f"— {labels[key]} —\n" + _run_cmd(cmd, None, 5))

    grab("os", "uname -a; getprop ro.product.model 2>/dev/null; "
               "getprop ro.build.version.release 2>/dev/null")
    grab("cpu", "nproc; grep -m1 'model name' /proc/cpuinfo 2>/dev/null; "
                "getprop ro.soc.manufacturer 2>/dev/null")
    grab("mem", "free -h 2>/dev/null || head -3 /proc/meminfo")
    grab("disk", "df -h / /sdcard 2>/dev/null")
    grab("net", "hostname -I 2>/dev/null; "
                "ip -4 route get 1 2>/dev/null | head -1")
    if not out:
        return f"section tidak dikenal: {section} (all|os|cpu|mem|disk|net)"
    return "\n\n".join(out)


TOOL_IMPL = {
    "bash": tool_bash,
    "read": tool_read,
    "write": tool_write,
    "edit": tool_edit,
    "glob": tool_glob,
    "grep": tool_grep,
    "websearch": tool_web_search,
    "webfetch": tool_web_fetch,
    "device": tool_device,
    "tree": tool_tree,
    "build": tool_build,
    "debug": tool_debug,
    "logs": tool_logs,
    "git": tool_git,
    "pkg": tool_pkg,
    "scaffold": tool_scaffold,
    "sdk": tool_sdk,
    "ssh": tool_ssh,
    "download": tool_download,
    "serve": tool_serve,
    "bg": tool_bg,
    "root": tool_root,
    "media": tool_media,
    "screenshot": tool_screenshot,
    "sys": tool_sys,
}
# tool yang dianggap aman (tidak mengubah sistem) — tetap dikonfirmasi tapi
# ditandai di prompt
SAFE_TOOLS = {"read", "glob", "grep", "websearch", "webfetch", "tree", "logs",
              "sys"}


