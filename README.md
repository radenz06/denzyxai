# denzyx AI

Asisten AI yang jalan di terminal (Termux / Linux). Berisi TUI chat, akses
perangkat Android, auto-reply 24 jam, plus web member area dan bot Telegram
untuk mengelola langganan.

## Fitur

- TUI chat dengan navigasi keyboard (curses)
- Tool calling: shell, file, web, git, build, dan lain-lain
- Web member area: registrasi, login, chat via browser
- Bot Telegram: notifikasi, konfirmasi pembayaran, kelola member
- Auto-reply notifikasi 24 jam (termux-api)
- Voice chat (STT/TTS via termux-api, opsional)
- Lisensi berpassword
- Pengelolaan langganan member via QR pembayaran

## Install

```sh
git clone https://github.com/radenz06/denzyxai.git
cd denzyxai
./install.sh
```

## Pakai

```sh
./denzyx          # masuk ke TUI
./denzyx --help   # opsi baris perintah
python3 admin-denz.py   # panel owner (kelola member, setup, restart)
python3 auto-denz.py install   # auto-reply 24 jam
./denzyx --voice  # voice chat (butuh termux-api)
```

Dependency: `python3` + `curses` (bawaan). Fitur perangkat/auto-reply
butuh package `termux-api`. Web butuh `cloudflared` untuk tunnel.

## Struktur

```
denzyx.py        # app utama (TUI)
dscli.py         # library tool-calling
webdenz.py       # web member area
admin-denz.py    # panel owner CLI
denzbot.py       # bot Telegram
auto-denz.py     # daemon 24 jam
voice-denz.py    # voice chat
payocr.py        # OCR bukti pembayaran
lic.py           # lisensi
securecfg.py     # config terenkripsi
track.py         # pencatatan aktivitas
waf.py           # proteksi akses
web-frontend/    # frontend web
tests/           # pytest
```

Konfigurasi lewat `webconfig.json` (lihat `config.example.json`) atau env
var. Lihat `CONTRIBUTING.md` untuk panduan berkontribusi.

## Lisensi

MIT — lihat [LICENSE](LICENSE).
