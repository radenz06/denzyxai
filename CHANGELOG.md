# Changelog

Semua perubahan penting dicatat di sini. Format mengikuti
[Keep a Changelog](https://keepachangelog.com/id-ID/1.1.0/) dan
[Semantic Versioning](https://semver.org/lang/id/).

## [2.3.0] - 2026-08-07

### Ditambahkan
- **Voice chat** (`voice-denz.py` + menu "Voice Chat" + `./denzyx --voice`):
  call & ngobrol langsung dengan AI. Dengar pakai
  `termux-speech-to-text` (STT on-device Android, tanpa server), AI
  jawab disuarakan lewat `termux-tts-speak`. Opsi `--lang`, `--rate`,
  `--pitch`, `--wake <kata>`, `--no-tts`, `--listen-once`. Percakapan
  tersimpan ke `sessions/` dan muncul di riwayat TUI.
- CLI `./denzyx --help`.

## [2.2.0] - 2026-08-07

### Ditambahkan
- Tool baru (total 26):
  - `ssh` — eksekusi perintah + transfer file (scp) ke mesin lain.
  - `download` — unduh file dengan curl/wget (retry + resume).
  - `serve` — server HTTP buat berbagi file ke perangkat lain di LAN,
    jalan di background, bisa start/stop/status.
  - `bg` — manajemen job background (nohup): start/list/tail/kill.
  - `root` — eksekusi perintah dengan akses root (su) kalau perangkat
    di-root.
  - `media` — play/stop audio-video, rekam suara, info file (ffprobe).
  - `screenshot` — tangkap layar HP (termux-api).
  - `sys` — info sistem sekilas (OS, CPU, memori, disk, IP).

## [2.1.0] - 2026-08-07

### Ditambahkan
- Tool `sdk`: kelola Android SDK dari dalam chat — `check`, `setup`,
  `install`, `list`, `adb`. Setup install commandline-tools, platform-tools,
  dan semua versi platform sekaligus (fallback kalau `compileSdk` beda-beda).
- Tool `tree`, `build`, `debug`, `logs`, `git`, `pkg`, `scaffold` —
  lengkapin kerjaan bikin aplikasi + debugging dari satu tempat.
- Deteksi tipe proyek otomatis (Python, Node, Go, Rust, C/C++, Gradle,
  Maven) termasuk fallback dari ekstensi file.
- Infrastruktur repo: README, LICENSE (MIT), CHANGELOG, `install.sh`,
  `scripts/backup.sh`, `tests/`, workflow CI, `.editorconfig`,
  `.gitattributes`, `.env.example`.
- Shortcut Ctrl+Alt+D untuk buka app instan (via `.bashrc` + `.blerc`).

### Diubah
- Tool `device` diperluas (35 aksi: battery, clipboard, notifikasi, torch,
  SMS, kontak, lokasi, kamera, dll).
- Auto-daemon: deteksi koneksi internet + antrian balasan saat offline,
  lalu lanjut otomatis pas online lagi (pakai `socket` ke host API, cache
  5 detik).
- Build Android diarahkan ke `aapt2`/`apksigner`/`zipalign` native Termux
  (build-tools Google x86_64 nggak jalan di perangkat aarch64).

## [2.0.0] - 2026-08-06

### Ditambahkan
- Rebrand `wormGPTxARIF` → `denzyx AI` (panggilan Denz).
- Tema baru: cyan/biru gelap, amoled, bisa di-override via `theme.md`.
- Efek matrix rain di menu utama, riwayat sesi, dan sidebar chat.
- Layar statistik baru: dashboard animasi + jam WIB live.
- Tool `device` untuk akses Termux-API.
- `auto-denz.py`: daemon auto-reply notifikasi + alert baterai + wake-lock.

### Diubah
- Persona system prompt dirombak total (patuh, to the point, gaul ala
  Jaksel) — dimuat fresh dari file tiap request.

## [1.0.0] - 2026-07

- Versi awal `wormGPTxARIF`: TUI chat + tool calling dasar.
