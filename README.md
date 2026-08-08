# denzyz AI

AI agent buat Termux yang jalan di terminal. Punya menu interaktif (TUI),
akses langsung ke perangkat Android, bisa bales notifikasi sendiri 24 jam,
dan cukup lengkap buat bikin aplikasi — dari Python sampe Android APK.

```text
┌─ denzyx AI ─────────────────────────────────┐
│                                             │
│   🛰  >  chat                               │
│       riwayat sesi                          │
│       statistik & konteks                   │
│       ...
│                                             │
└─────────────────────────────────────────────┘
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
git clone https://github.com/radenz06/denzyz.git
cd denzyz
./install.sh
```

Atau tanpa clone, cukup pastikan folder berisi `denzyx.py`, terus:

```sh
./denzyx          # masuk ke TUI
./denzyx --help   # opsi baris perintah
```

Dependency utama: `python3` dan `curses` (bawaan). Untuk fitur perangkat
butuh package `termux-api`. Untuk auto-daemon butuh `termux-api` juga
(biar `termux-notification-list` bisa baca notifikasi).

## Pakai

| Aksi | Cara |
|---|---|
| Buka app | `./denzyx` (atau `denzyx` kalau sudah di-`install.sh`) |
| Shortcut instan | Ctrl+Alt+D (lewat `.bashrc` + `.blerc`) |
| Voice chat | menu "Voice Chat" atau `./denzyx --voice` |
| Pindah menu | ↑/↓, j/k |
| Pilih / kirim | Enter |
| Kembali / batal | ESC |
| Keluar | Ctrl-C |
| Auto-reply 24 jam | `python3 auto-denz.py install` lalu `status` |
| Cek status SDK Android | tool `sdk` di dalam chat |

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
- **Env var auto-daemon** (`DENZYX_AUTO_*`) — interval, balas AI on/off,
  TTS, vibrate, ambang baterai, app yang di-ignore, TTL dedupe. Lihat
  `.env.example`.

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
denzyz/
├── denzyx.py          # app utama (TUI curses + loop tool calling)
├── dscli.py           # library tool-calling (schema + implementasi)
├── auto-denz.py       # daemon 24 jam (notifikasi, auto-reply, baterai)
├── voice-denz.py      # panggilan suara (STT + TTS via termux-api)
├── system_prompt.md   # persona & aturan AI
├── theme.md           # warna TUI
├── denzyx             # launcher (resolver folder + python path)
├── install.sh         # setup sekali jalan
├── scripts/           # helper (backup, dll)
├── tests/             # pytest
└── sessions/          # riwayat sesi (gitignored)
```

## Troubleshooting

- **`termux-notification-list` kosong / butuh izin** — Settings → Apps →
  Special access → Notification access → aktifkan Termux:API.
- **Tool device error "permission"** — grant izin lewat dialog layar yang
  muncul sekali.
- **Daemon mati** — `python3 auto-denz.py status`; job scheduler (ID 745)
  auto-restart tiap 15 menit, plus boot script di `.termux/boot/`.
- **Tema rusak** — hapus `theme.md`, balik ke default.

## Lisensi

MIT — lihat [LICENSE](LICENSE). Dibuat oleh radenz06.
