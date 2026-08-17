<p align="center">
  <img src="docs/logo.jpg" width="120" height="120" style="border-radius:28px" alt="denzyx AI">
</p>

<p align="center">
  <img src="docs/banner.svg" width="100%" alt="denzyx AI banner">
</p>

<p align="center">
  <a href="https://www.python.org/"><img src="https://img.shields.io/badge/Python-3.10+-3776AB?style=for-the-badge&logo=python&logoColor=white" alt="Python"></a>
  <a href="https://github.com/radenz06/denzyxai/blob/main/LICENSE"><img src="https://img.shields.io/badge/License-MIT-green?style=for-the-badge" alt="License"></a>
  <a href="https://www.gnu.org/software/bash/"><img src="https://img.shields.io/badge/Platform-Termux/Linux-4F7CFF?style=for-the-badge&logo=linux&logoColor=white" alt="Platform"></a>
  <img src="https://img.shields.io/badge/Status-Active-22c55e?style=for-the-badge" alt="Status">
</p>

> **denzyx AI** — asisten AI all-in-one yang jalan di Termux/Linux: TUI chat dengan tool calling, web member area bergaya DeepSeek, bot Telegram, dan auto-reply 24 jam. Dibangun modular supaya gampang dikembangkan jadi apapun.

---

## 🚀 Project Overview

denzyx AI adalah ekosistem AI personal yang lengkap: ngobrol di **terminal** dengan tool calling (shell, file, web, git, build), akses **perangkat Android** (baterai, GPS, SMS, kontak, notifikasi), **web chat** modern dengan streaming, plus **bot Telegram** buat ngelola langganan member.

**Primary goals:**
- Satu AI untuk semua permukaan: terminal, web, Telegram, voice
- Tool calling yang aman & terkontrol (legal-only pentest, guard anti akses sistem di web)
- Member area berbayar dengan QR pembayaran + konfirmasi otomatis (OCR)
- Semua data lokal & terenkripsi (Fernet), tanpa cloud pihak ketiga

---

## 🎯 Key Features

| Area | Fitur |
|---|---|
| 🖥️ **TUI Terminal** | Chat curses, navigasi keyboard, theme kustom, streaming delta, reasoning |
| 🛠️ **Tool Calling** | Shell, file, web, git, build, sdk Android, device (HP), locate (GPS), pentest legal |
| 🌐 **Web Chat** | UI ala DeepSeek: welcome screen, bubble + salin, streaming + stop, markdown, panel model sendiri |
| 📱 **Akses HP** | Baterai, clipboard, notifikasi, torch, volume, SMS, kontak, lokasi (via Termux-API) |
| 🤖 **Bot Telegram** | Notifikasi, konfirmasi pembayaran, kelola member, auto-reply |
| ⏰ **Auto-reply 24 jam** | Daemon `auto-denz.py` yang nanggepin notifikasi otomatis |
| 🎙️ **Voice Chat** | STT/TTS via Termux-API (opsional) |
| 💳 **Langganan** | QR pembayaran, OCR bukti transfer (`payocr.py`), masa aktif per member |
| 🔐 **Keamanan** | WAF blokir IP, CSRF, config & data terenkripsi (Fernet), lisensi berpassword |

---

## ▶️ Demo Aplikasi — Visual Mockup

> Mockup SVG di bawah ini render langsung di GitHub. Versi aslinya bisa dicoba lewat terminal atau web member area.

### 🌐 Web Chat (halaman utama member)

<p align="center">
  <img src="docs/mockup-chat.svg" width="100%" alt="Web chat mockup">
</p>

### 🖥️ Terminal TUI

<p align="center">
  <img src="docs/mockup-terminal.svg" width="100%" alt="Terminal TUI mockup">
</p>

### 📱 Tampilan Mobile

<p align="center">
  <img src="docs/mockup-phone.svg" width="280" alt="Phone mockup">
</p>

---

## 🧰 Tech Stack

| Komponen | Teknologi |
|---|---|
| **Core** | Python 3.10+ · curses (TUI) · ThreadingHTTPServer (web) |
| **Web** | HTML/CSS/JS vanilla — zero framework, glassmorphism + 3D CSS |
| **AI Client** | OpenAI-compatible streaming (zen API), retry otomatis, fallback FREE |
| **Telegram** | Bot API · OCR bukti bayar (`payocr.py`) |
| **Enkripsi** | `cryptography` Fernet (config, member data, password) |
| **Tunnel** | `cloudflared` (web diakses dari luar) |
| **Device** | `termux-api` (GPS, sensor, telepon, notifikasi) |

---

## 📁 Project Structure

```
denzyx-ai/
├─ denzyx.py        # app utama — TUI chat + tool calling + AI client
├─ dscli.py         # library tool-calling (shell, file, web, git, build)
├─ webdenz.py       # web member area — chat, auth, langganan, API
├─ admin-denz.py    # panel owner CLI (kelola member, setup, restart)
├─ denzbot.py       # bot Telegram
├─ auto-denz.py     # daemon auto-reply 24 jam
├─ voice-denz.py    # voice chat (STT/TTS)
├─ payocr.py        # OCR bukti pembayaran
├─ lic.py           # lisensi berpassword
├─ securecfg.py     # config terenkripsi (Fernet)
├─ track.py         # pencatatan aktivitas & visitor
├─ waf.py           # proteksi akses / blokir IP
├─ system_prompt.md # persona & instruksi AI (terminal)
├─ web-frontend/    # frontend web statis
├─ docs/            # logo + mockup README
└─ tests/           # pytest
```

---

## ⚙️ Install

```sh
git clone https://github.com/radenz06/denzyxai.git
cd denzyxai
./install.sh
```

**Dependency:**
- `python3` + `curses` (bawaan Python)
- `termux-api` — untuk fitur perangkat/auto-reply (Termux)
- `cloudflared` — untuk tunnel web
- `cryptography`, `requests` — via `install.sh`

---

## 📖 Pakai

### Terminal (TUI)

```sh
./denzyx              # masuk ke TUI
./denzyx --help       # opsi baris perintah
./denzyx --voice      # voice chat (butuh termux-api)
```

### Web member area

```sh
python3 webdenz.py            # jalan di http://localhost:8000
# akses dari luar:
cloudflared tunnel --url http://localhost:8000
```

Fitur web: registrasi + login member, chat AI streaming, panel model AI sendiri
(API key & endpoint milik user), status langganan, bayar via QR.

### Panel owner & daemon

```sh
python3 admin-denz.py            # kelola member, setup, restart
python3 auto-denz.py install     # auto-reply 24 jam
python3 denzbot.py               # bot Telegram
```

---

## 🛡️ Keamanan & Etika

- Web chat memakai **system prompt khusus**: user yang minta akses server/terminal
  ditolak mentah-mentah (`ACCESS DENIED`) — web hanya untuk ngobrol biasa
- Tool pentest hanya berjalan **legal & berizin** (target milik sendiri / kontrak),
  dengan cleanup otomatis dan tanpa merusak
- Config, password, dan data member **terenkripsi** (Fernet), tidak ada data di cloud
- WAF bawaan memblokir IP mencurigakan; semua form pakai CSRF token

---

## 🤝 Kontribusi

Pull request dipersilakan. Untuk perubahan besar, buka issue dulu ya.
Lihat [CONTRIBUTING.md](CONTRIBUTING.md) untuk panduan.

---

## 📜 Lisensi

MIT — lihat [LICENSE](LICENSE).

<p align="center">
  <sub>dibuat dengan 🖤 oleh <a href="https://github.com/radenz06">radenz06</a></sub>
</p>