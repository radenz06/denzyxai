# denzyx AI

AI agent buat Termux yang jalan di terminal — plus **web member area**,
**bot Telegram**, dan **lisensi berpassword**. Punya menu interaktif (TUI),
akses langsung ke perangkat Android, bisa bales notifikasi sendiri 24 jam,
dan cukup lengkap buat bikin aplikasi — dari Python sampe Android APK.

```text
┌─ denzyx AI ─────────────────────────────────┐
│                                                   │
│   🛰  >  chat                                     │
│       riwayat sesi                                │
│       statistik & konteks                         │
│       ...                                         │
│                                                   │
└───────────────────────────────────────────┘
```

Tanpa API key pun jalan — otomatis turun ke free tier. Key (kalau punya)
dibaca dari file auth standard, bukan dihardcode.

## Kenapa ada

Dibikin biar ada satu tempat ngobrol + nyuruh AI ngapa-ngapain langsung
dari HP, tanpa keluar ke app lain. Semua ngerti bahasa Indonesia gaul,
tapi serius kalau disuruh kerja.

## Fitur utama

- **TUI pakai curses** — navigasi panah/j/k, resize aman, tema AMOLED
  yang bisa diganti dari `theme.md` tanpa buka kode.
- **System prompt dari file** — edit `system_prompt.md`, langsung kepakai
  di sesi berikutnya, nggak perlu restart.
- **Sesi tersimpan otomatis** — riwayat chat + context di `sessions/`.
- **Tool calling lengkap (27 tool)** — bash, baca/tulis/edit file, glob,
  grep, web search/fetch, task (sub-agent), device (Termux-API), git,
  build, debug, logs, pkg, scaffold, tree, sdk (Android), plus akses
  ekstra: ssh, download, serve (HTTP server), bg (job background),
  root (su), media, screenshot, dan sys.
- **Web member area** (`webdenz.py`) — registrasi + login member,
  chat AI dari browser (streaming + markdown), status langganan, ganti
  password. Langganan dibayar via **QR**
  (dari `/storage/emulated/0/qr.jpg`), setelah regist langsung muncul QR
  + tombol konfirmasi ke owner di Telegram. Auto-connect internet
  lewat **cloudflared** (link baru tiap run, dijamin selalu bisa diakses).
  **Keamanan**: CSRF token di semua form, rate limit login/register/chat,
  cookie `HttpOnly; SameSite=Lax` (+ `Secure` saat HTTPS), owner panel
  dengan pencarian & pagination. HTTPS opsional via `ssl_cert`/`ssl_key`.
- **Owner panel** (`admin-denz.py`) — kelola member (activate/ban/extend),
  lihat log registrasi, setup config, restart server/bot/tunnel, dan
  ganti password lisensi. Member dengan role **admin (reseller)** bisa
  menambah member lewat web (`/admin/add`) atau bot (`/addmember`).
- **Bot Telegram** (`denzbot.py`) — notifikasi registrasi & login member,
  kirim QR, dan aktivasi ke owner. **Daftar 2 metode**: registrasi di web,
  atau request langsung dari bot (`/daftar <username> <password>`) → owner
  tinggal `/approve <username>` dari chat. Owner juga bisa langsung
  `/addmember <user> <pass> [hari]` dan `/addadmin <user>`.
- **Lisensi berpassword** (`lic.py`) — project **nggak bisa dijalankan
  tanpa password lisensi**. Password ter-hash (PBKDF2), nggak pernah
  disimpan plaintext. Owner ganti lewat `python3 admin-denz.py setpass`.
- **Voice chat** (`voice-denz.py`) — panggilan suara **adaptif**: AI
  ikut ritme & mood bicaramu, bisa ganti suara saat disuruh (cowok/
  cewek/anak kecil), dan dengar walau sedang ngomong (barge-in).
  STT jernih (faster-whisper) + suara cewe natural (Google TTS →
  edge-tts → TTS Android). Buka lewat menu "Voice Chat" atau
  `./denzyx --voice`. Percakapan ikut tersimpan di riwayat sesi.
- **Mode auto 24 jam** (`auto-denz.py`) — baca notifikasi, bales pakai AI
  (gaya persona yang sama), vibrate/TTS, monitor baterai, tahan hidup
  lewat termux-job-scheduler + boot script, dan nggak mati waktu offline
  (antre balasan, lanjut pas online lagi).
- **Bisa build Android** — tool `sdk` buat install/kelola Android SDK
  (semua versi platform), tool `build` yang detect otomatis tipe proyek.

## Install

```sh
git clone https://github.com/radenz06/denzyxai.git
cd denzyxai
./install.sh
```

Atau tanpa clone, cukup pastikan folder berisi `denzyx.py`, terus:

```sh
./denzyx          # masuk ke TUI (minta password lisensi)
./denzyx --help   # opsi baris perintah
```

> **Lisensi:** pertama kali jalan, terminal bakal minta **password
> lisensi**. Tanpa password yang benar, program nggak mau jalan.
> Password default dibagikan owner saat beli lisensi — segera ganti
> dengan `python3 admin-denz.py setpass` (butuh password lama + terminal
> interaktif). Setelah password benar sekali, mesin ini otomatis dapat
> token lokal (`webdata/.lic_ok`, gitignored) buat daemon/restart tanpa
> prompt. Ganti password = token lama otomatis tidak berlaku.

Dependency utama: `python3` dan `curses` (bawaan). Untuk fitur perangkat
butuh package `termux-api`. Untuk auto-daemon butuh `termux-api` juga
(biar `termux-notification-list` bisa baca notifikasi). Untuk web/tunnel
butuh `python3` + `cloudflared` + `cryptography`.

## Pakai

| Aksi | Cara |
|---|---|
| Buka app | `./denzyx` (minta password lisensi dulu) |
| Shortcut instan | Ctrl+Alt+D (lewat `.bashrc` + `.blerc`) |
| Voice chat | menu "Voice Chat" atau `./denzyx --voice` |
| Web member (auto-link) | `./denzyx` → otomatis start web+bot+tunnel; link muncul di terminal |
| Registrasi web | buka link tunnel lalu `/register` |
| Owner panel | `python3 admin-denz.py` (atau `./denzyx` lalu pilih Owner Panel) |
| Ganti password lisensi | `python3 admin-denz.py setpass` |
| Pindah menu | ↑/↓, j/k |
| Pilih / kirim | Enter |
| Kembali / batal | ESC |
| Keluar | Ctrl-C |
| Auto-reply 24 jam | `python3 auto-denz.py install` lalu `status` |
| Cek status SDK Android | tool `sdk` di dalam chat |

## Web member area

Jalan bareng AI — pas `./denzyx` dilaunch, server web + bot + tunnel
cloudflared otomatis start (kecuali `--noweb`).

1. **Auto-link** — tunnel cloudflared bikin link publik baru (mis.
   `https://xxxx.trycloudflare.com`). Link + status tercetak di terminal.
2. **Registrasi** — user buka `/register`, isi username/password, terus
   bayar. Setelah regist langsung tampil **QR pembayaran** di atas
   (dari `/storage/emulated/0/qr.jpg`) + tombol **"Konfirmasi Aktivasi
   ke Telegram"** yang blank ke DM owner (`t.me/colipopi`).
3. **Aktivasi** — owner aktifkan dari owner panel (`admin-denz.py` menu
   `3. Activate member`) atau via bot. Status member: `pending` →
   `active` (masa aktif sesuai `sub_days`) → `expired` → `banned`.
4. **Chat** — member login dan ngobrol dengan AI langsung dari browser
   (`/chat`), cek status langganan di `/status`.

Punya frontend terpisah buat deploy di Vercel (`web-frontend/`) yang
nembak API JSON (`/api/register`, `/api/login`, `/api/chat`, `/api/status`,
`/api/me`).

### Admin CLI (`admin-denz.py`)

```sh
python3 admin-denz.py            # menu interaktif (status, member, setup, dsb)
python3 admin-denz.py setpass    # ganti password lisensi
python3 admin-denz.py ensure     # pastikan server+bot+tunnel jalan
python3 admin-denz.py restart    # restart semua
python3 admin-denz.py start-bot  # / stop-bot / start-server / stop-server
python3 admin-denz.py start-tunnel  # / stop-tunnel / url
python3 admin-denz.py list       # daftar member
python3 admin-denz.py status     # status server/bot/tunnel/member
```

## Voice chat

Panggilan suara **adaptif**: AI ikut ritme & mood bicaramu, ganti suara
saat disuruh, dan bisa dengar walau sedang ngomong.

Kamu ngomong → **STT jernih** (faster-whisper neural offline, model
`base`, bahasa Indonesia — fallback `termux-speech-to-text`) → AI
membaca ritme (kecepatan), nada, dan mood-mu (ketawa/nangis/marah dari
analisis audio + kata) → jawab ringkas dengan **suara cewe natural**
(Google TTS → edge-tts `id-ID-GadisNeural` → TTS Android), kecepatan
& nada ikut menyesuaikan. Bilang "stop" buat menutup panggilan.

```sh
pip install faster-whisper numpy   # sekali — STT jernih + analisis mood/ritme
./denzyx --voice                # dari menu: pilih "Voice Chat"
./denzyx --voice --lang id-ID   # bahasa STT + TTS (default id-ID)
./denzyx --voice --stt whisper  # paksa STT whisper (atau --stt termux)
./denzyx --voice --stt-model small    # model whisper lebih akurat (lebih lambat)
./denzyx --voice --engine google      # google | edge | android | auto
./denzyx --voice --no-barge-in  # matikan dengar sambil ngomong
./denzyx --voice --no-learn     # matikan profil belajar ritme/nada
./denzyx --voice --wake denz    # cuma respons kalau dipanggil "denz"
./denzyx --voice --no-tts       # mode bisu: cuma teks
```

Perintah suara (bilang begitu, dan AI-nya ganti): **"pakai suara
cowok/cewek/anak kecil"**, **"lebih cepat / lebih lambat"**, **"suara
serak / suara tinggi / melengking"**, **"kembali normal"**. AI belajar
ritme & nada kamu ke `.denzyx/voice_profile.json` (matikan dengan
`--no-learn`).

`--engine auto` mencoba Google TTS (tanpa dependency) → edge-tts →
TTS Android. Google TTS & edge-tts butuh internet; tanpa internet,
otomatis turun ke TTS Android bahasa Indonesia. Butuh package
`termux-api` + izin mikrofon buat Termux. Percakapan tersimpan seperti
chat biasa dan muncul di "Riwayat Sesi".

## Konfigurasi

Semua lewat file atau env var, nggak perlu edit kode:

- **`system_prompt.md`** — kepribadian & aturan AI. Dibaca fresh tiap
  request.
- **`theme.md`** — warna TUI. Format `nama = fg,bg` (0–255, `-1` = default
  terminal). Hapus/rename file buat balik ke default.
- **`webconfig.json`** (gitignored) — token bot TG, chat id owner,
  username owner, harga langganan, masa aktif, `qr_path`, secret.
  Contoh kosong ada di `config.example.json`.
- **Env var auto-daemon** (`DENZYX_AUTO_*`) — interval, balas AI on/off,
  TTS, vibrate, ambang baterai, app yang di-ignore, TTL dedupe. Lihat
  `.env.example`.
- **Env var lisensi** — `DENZYX_PASS` (password lisensi sekali pakai buat
  non-interaktif) dan `DENZYX_LIC=ok` (sudah terverifikasi sesi ini).

## Tool

Lengkap buat nyelesaiin kerjaan beneran:

```
bash  read  write  edit  glob  grep          # kerja file & shell
websearch  webfetch                          # riset
task                                          # sub-agent otonom
device                                        # battery, notif, SMS, dll (Termux-API)
locate                                        # cek lokasi: GPS (Termux-API) + geolocation IP
tree                                          # struktur proyek
build  test  run                              # build otomatis per tipe proyek
debug  logs                                   # debugging & analisa log
git                                           # version control
pkg                                           # dependency (npm/pip/go/cargo)
scaffold                                      # template proyek baru
sdk                                           # Android SDK (build APK)
ssh                                           # eksekusi & transfer ke mesin lain
download                                      # unduh file (curl/wget, resume)
serve                                         # server HTTP buat bagi file (LAN)
bg                                            # job background + list/tail/kill
root                                          # perintah dengan akses root (su)
media                                         # play/stop/record/info multimedia
screenshot                                    # tangkap layar HP
sys                                           # info sistem (OS/CPU/mem/disk/net)
```

Kebijakan keamanan: tool baca-only (`read`, `glob`, `grep`, `websearch`,
`webfetch`, `tree`, `logs`, `sys`, `locate`) ditandai aman. Tool lain diminta
konfirmasi di TUI kecuali `auto_allow` aktif.

## Build aplikasi Android

1. Di dalam chat: `sdk(action='setup')` — install commandline-tools,
   platform-tools, dan semua platform.
2. `scaffold(type='android', ...)` atau `write` file proyek Gradle.
3. `build(target='build')` → `gradle assembleDebug`, hasil APK di
   `app/build/outputs/apk/`.

> Catatan perangkat aarch64: build-tools Google aslinya binary x86_64 dan
> nggak jalan di Termux. Pakai `aapt2`/`apksigner`/`zipalign` native yang
> sudah terinstall di Termux (diarahkan lewat `local.properties` /
> `android.aapt2FromMavenOverride`).

## Struktur

```text
denzyxai/
├── denzyx.py          # app utama (TUI curses + loop tool calling + auto web)
├── dscli.py           # library tool-calling (schema + implementasi)
├── webdenz.py         # web member area (registrasi, login, chat, status)
├── admin-denz.py      # owner panel CLI + restart server/bot/tunnel
├── denzbot.py         # bot Telegram (notifikasi, QR, aktivasi)
├── auto-denz.py       # daemon 24 jam (notifikasi, auto-reply, baterai)
├── voice-denz.py      # panggilan suara (STT + TTS via termux-api)
├── lic.py             # gerbang lisensi password (PBKDF2 hash)
├── system_prompt.md   # persona & aturan AI
├── theme.md           # warna TUI
├── config.example.json# contoh config web (nilai asli di webconfig.json)
├── web-frontend/      # frontend Vercel (deploy terpisah)
├── denzyx             # launcher (resolver folder + python path)
├── install.sh         # setup sekali jalan
├── scripts/           # helper (backup, dll)
├── tests/             # pytest
└── sessions/          # riwayat sesi (gitignored)
```

File sensitif (`webconfig.json`, `webdata/`, `sessions/`, `.env`,
`.denzyx/`) otomatis di-gitignore — nggak ikut ke GitHub.

## Troubleshooting

- **"Project terkunci: butuh password lisensi"** — masukkan password
  lisensi yang benar. Lupa? Minta reset ke owner. Setelah benar sekali,
  token lokal dibuat (`webdata/.lic_ok`) biar daemon/restart nggak nanya
  lagi. Owner: `python3 admin-denz.py setpass`.
- **`termux-notification-list` kosong / butuh izin** — Settings → Apps →
  Special access → Notification access → aktifkan Termux:API.
- **Tool device error "permission"** — grant izin lewat dialog layar yang
  muncul sekali.
- **Daemon mati** — `python3 auto-denz.py status`; job scheduler (ID 745)
  auto-restart tiap 15 menit, plus boot script di `.termux/boot/`.
- **Web nggak kebuka dari HP lain** — pastikan tunnel jalan
  (`python3 admin-denz.py status`); kalau mati, `python3 admin-denz.py start-tunnel`.
- **QR pembayaran nggak muncul** — cek file QR ada di
  `/storage/emulated/0/qr.jpg` (bisa juga `/sdcard/qr.jpg`).
- **Tema rusak** — hapus `theme.md`, balik ke default.

## Lisensi

MIT — lihat [LICENSE](LICENSE). Dibuat oleh radenz06.
