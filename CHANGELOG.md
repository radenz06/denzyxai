# Changelog

Semua perubahan penting dicatat di sini. Format mengikuti
[Keep a Changelog](https://keepachangelog.com/id-ID/1.1.0/) dan
[Semantic Versioning](https://semver.org/lang/id/).

## [3.3.3] - 2026-08-12

### Ditambahkan
- **Pengingat langganan hampir habis**: bot otomatis memberitahu owner
  (`⏰ N langganan hampir habis` + daftar H-2/H-1) dan member langsung via
  TG setiap 6 jam. Tidak spam — sekali per masa aktif (state di
  `webdata/.remind_expire.json`).
- **Notifikasi login member detail**: kini sama lengkapnya dengan notifikasi
  registrasi (IP publik/private + peer + CF/XFF, geolokasi & ISP, browser ·
  OS · device · engine, waktu lengkap, referer) lewat `_event_notify`.
- Default `host` dipakai: `127.0.0.1` (akses hanya lewat tunnel, bukan
  terbuka ke jaringan).
- Regression test: `test_login_notify_detail`, `test_remind_expiring`.

## [3.3.2] - 2026-08-12

### Ditambahkan
- **Notifikasi registrasi TG detail** (`_reg_notify`): tak lagi cuma IP —
  kini berisi username/nama, tanggal-jam-tahun + zona waktu, IP publik
  (klasifikasi Publik/Private/Loopback), IP peer + CF-IP + rantai XFF,
  software/perangkat dari User-Agent (browser · OS · device · engine),
  status bot, referer, dan geolokasi + ISP (lokasi, org, tipe koneksi).
  Dikirim di thread background (tidak memperlambat response).
- Regression test `test_reg_notify_detail`.

## [3.3.1] - 2026-08-12

### Diperbaiki
- **Data-loss parah**: `load_config()` pernah menimpa `webconfig.json` dengan
  nilai default + secret baru saat decrypt gagal (key berubah/file rusak),
  sehingga token TG, password owner, QR & secret lama hilang. Kini secret
  baru hanya dibuat saat config berhasil dibaca atau file belum ada.
- **`lic.setpass()`** tak lagi menghapus isi config lain bila `webconfig.json`
  tidak terbaca (sebelumnya hanya menulis `{lic}` ke file).
- Regression test untuk kedua bug di atas ditambahkan.

## [3.3.0] - 2026-08-11

### Ditambahkan
- **Pelacak pengunjung lengkap** (`track.py` + `webdenz.py`): setiap request
  terekam — **IP public & private** (peer socket, `CF-Connecting-IP`, rantai
  `X-Forwarded-For`/`X-Real-IP`, klasifikasi public/private/loopback),
  **lokasi & ISP** (ipwho.is, async + cache, tanpa memblokir request),
  **software** (User-Agent di-parse → browser, OS, device, engine; bot &
  scanner seperti sqlmap/nikto/curl terdeteksi), path yang dikunjungi,
  metode, kode status response, referer, pertama/terakhir kunjungan.
  Disimpan di `webdata/visitors.json` (agregat per IP, tulis atomik,
  flush 10 dtk) + `webdata/logs/visitors.log` (riwayat, 1 baris/menit/IP).
- **Owner panel `/owner/visitors`**: tabel pengunjung (badge PUBLIC/PRIVATE/
  LOOPBACK/BOT/BANNED), pencarian IP/browser/OS/lokasi, ringkasan (total,
  hari ini, aktif 24 jam, kunjungan, bot, mobile), **detail visitor**
  `/owner/visitor/<ip>` (semua data + riwayat + ban/unban), tombol
  ⛔ Ban & 🗑️ hapus semua data (CSRF-protected).
- **CLI**: `admin-denz.py visitors [ip|cari]` (daftar/detail) dan
  `visitors-clear`; menu interaktif `[V]` & `[W]`; ringkasan di status.
- Config baru: `track_visitors` & `track_geo` (geolokasi bisa dimatikan;
  di test juga via env `WEBDENZ_TRACK_GEO=0`).

## [3.2.0] - 2026-08-11

### Ditambahkan
- **`securecfg.py` — konfigurasi terenkripsi at-rest**: `webconfig.json`
  kini disimpan TERENKRIPSI di disk (Fernet; key 32-byte random di
  `webdata/.config.key`, chmod 600, gitignored). File plaintext versi lama
  otomatis dimigrasi saat dibaca. `lic.py` ikut diperbarui agar baca/tulis
  config lewat `securecfg`. Bisa di-override via env `WEBDENZ_CFG_KEY`.
- **Halaman blokir WAF**: IP yang di-ban (atau terdeteksi serangan) mendapat
  halaman 403 berisi marquee **"KAMU BODOH BANGET SIH, JANGAN GITU YA LAIN
  KALI😹🖕"** — pesan dari denzyx.
- **Hardening TLS (vuln "Weak Cipher Suites")**: saat pakai
  `ssl_cert`/`ssl_key`, server hanya menerima **TLS 1.2+** dan memblokir
  cipher lemah (RC4, DES, 3DES, MD5, CBC, SHA-1, NULL/EXPORT, PSK/SRP/DSS,
  LOW/CAMELLIA/SEED/IDEA) — hanya ECDHE+AESGCM / ECDHE+CHACHA20 /
  DHE+AESGCM. `OP_CIPHER_SERVER_PREFERENCE` + kompresi TLS dimatikan
  (anti CRIME).

## [3.1.0] - 2026-08-11

### Ditambahkan — WAF (Web Application Firewall) `waf.py`
- **IP asli klien**: `CF-Connecting-IP` / `X-Forwarded-For` dipercaya HANYA
  dari koneksi loopback (koneksi dari cloudflared) — spoof header dari akses
  langsung tidak mempan. Rate limit/ban/log semuanya pakai IP asli.
- **Deteksi serangan → ban IP permanen** (tersimpan `webdata/bans.json`):
  User-Agent alat peretas (Burp, sqlmap, nikto, dll), honeypot path
  (wp-login.php, phpmyadmin, .git, dll), path traversal (`../`, `%2e%2e`),
  pola injection (SQLi/XSS/LFI), endpoint scan (404 ke ≥ `ban_scan_threshold`
  path acak dalam 60 dtk), brute-force login/owner/register
  (≥ `ban_fail_threshold` gagal/rate-limit dalam 600 dtk).
- **Notifikasi Telegram ke owner** saat ada IP diblokir: IP + **lokasi
  geografis** (ipwho.is, async + cache) + UA + path + waktu.
- **Kelola ban**: halaman owner `/owner/security` (daftar + unban), bot
  `/bans /unbanip <ip> /block <ip>`, CLI `admin-denz.py bans|unban|block`.
- **Hardening server**: socket timeout (anti slowloris), batas koneksi
  paralel per IP (`conn_max`), rate limit request umum per IP
  (`req_rate_max`), batas body POST (`max_body`), tolak `Host` berisi CR/LF,
  allowlist opsional `cors_origins` & `allowed_hosts`, header keamanan
  tambahan (CSP, `Permissions-Policy`, `Cross-Origin-Opener-Policy`), HSTS +
  `Secure` cookie otomatis saat lewat tunnel edge TLS, `Server` header
  disembunyikan.
- **Default bind 127.0.0.1** (akses hanya via tunnel) + peringatan saat
  `host` masih `0.0.0.0`; tunnel cloudflared pakai `--no-autoupdate`.
- IP loopback tidak pernah di-ban otomatis.
- `SECURITY.md` baru — dokumentasi lapisan keamanan.

## [3.0.1] - 2026-08-11

### Ditambahkan — owner panel
- **Hapus pengguna (delete)** — tombol `🗑️ Hapus Pengguna (permanen)` di
  detail member (berlaku untuk member maupun admin/reseller), dengan
  konfirmasi browser. Menghapus file member + file sesi `.md` + log
  aktivitas `delete`, lalu notifikasi ke Telegram. Sesuai saran: untuk
  sengketa pembayaran lebih baik **ban** dulu daripada delete (data tetap
  tersimpan).

## [3.0.0] - 2026-08-11

### Ditambahkan — webdenz v3 (keamanan + UX)
- **HTTPS.** `ssl_cert`/`ssl_key` di config — bila terisi & file ada, server
  jalan pakai TLS; cookie otomatis dapat flag `Secure`.
- **Cookie aman.** Semua cookie (`denz_member`, `denz_owner`, `denz_csrf`)
  sekarang `HttpOnly; SameSite=Lax` (+ `Secure` saat HTTPS).
- **CSRF.** Setiap form POST (login, register, owner, admin/add, password,
  logout, aksi owner) divalidasi token HMAC per-sesi yang dikirim lewat
  cookie `denz_csrf` + field `_csrf`; tanpa token → 403.
- **Rate limit** anti brute-force & abuse per-IP (login/registrasi/owner)
  dan per-member (chat). Konfigurasi: `rate_max_attempts`,
  `rate_window_sec`, `chat_rate_max`, `chat_rate_window`.
- **Logout jadi POST** (bukan GET) + dibersihkan cookienya.
- **Streaming chat** — `/api/chat/stream` (NDJSON chunked), jawaban AI
  muncul per potongan + render markdown di browser.
- **Render markdown server-side** untuk riwayat chat (aman, no XSS).
- **Fix XSS**: semua input member (username, nama, catatan, dll) di-escape.
- **Owner panel**: pencarian member & log + pagination (`owner_per_page`).
- **Ganti password** member (`/password`) & reset password dari owner panel.
- **Headers keamanan**: `X-Content-Type-Options`, `X-Frame-Options`,
  `Referrer-Policy`, `Cache-Control: no-store` untuk halaman dinamis,
  cache 1 jam untuk `/qr`.
- `owner_token_valid` pakai `secrets.compare_digest` (anti timing attack).
- Favicon, meta description.

## [2.7.0] - 2026-08-07

### Ditambahkan
- **Tool `locate` — cek lokasi lengkap.** Begitu user minta cek lokasi:
  (1) GPS dinyalakan via Termux-API (`termux-location -p gps`, fallback
  network/passive), (2) geolocation IP publik (ipwho.is → ip-api),
  (3) hasilnya disajikan sebagai daftar/list (`- latitude`, `- longitude`,
  akurasi, kota, negara, ISP, link maps). `locate(action='all'|'gps'|'ip')`.
- System prompt diarahkan supaya selalu pakai `locate` untuk cek lokasi
  dan menampilkan hasilnya sebagai list.

## [2.6.0] - 2026-08-07

### Ditambahkan — voice ADAPTIF
- **Ikut ritme bicaramu.** Kecepatan ngomong diukur dari rekaman
  (karakter/detik) → kecepatan bicara AI ikut nyesuain. Belajar makin
  pas tiap percakapan, profil disimpan di `.denzyx/voice_profile.json`.
- **Deteksi mood.** Ketawa/nangis/marah/ceria dideteksi dari analisis
  audio (energi + nada via numpy) dan dari kata (wkwk, huhu, dsb).
  AI nyetel kecepatan & nada jawaban, plus diminta jawab sesuai mood
  (menghibur, tegas, dsb). Butuh `pip install numpy`.
- **Ganti suara pas disuruh.** "pakai suara cowok" → `id-ID-ArdiNeural`
  (edge-tts), "suara cewek" → GadisNeural/Google, "suara anak kecil" →
  TTS Android nada tinggi. Juga "lebih cepat/lambat", "suara serak/
  tinggi/melengking", "kembali normal". Pilihan disimpan di profil.
- **Dengar sambil ngomong (barge-in).** Mic ikut merekam saat AI lagi
  ngomong; begitu kamu mulai bicara (energi jauh di atas echo TTS),
  playback langsung berhenti dan AI siap dengar lagi. Matikan dengan
  `--no-barge-in`.
- `--no-learn` buat nonaktifkan profil belajar.

## [2.5.0] - 2026-08-07

### Ditambahkan
- **STT jernih dengan faster-whisper.** Voice chat sekarang dengar pakai
  whisper neural offline (model `base` int8, bahasa Indonesia) — jauh
  lebih akurat daripada STT Android. `pip install faster-whisper`.
  Opsi `--stt auto|whisper|termux`, `--stt-model <size>` (default
  `base`), `--record-seconds` (default 6). Auto fallback ke
  `termux-speech-to-text` kalau whisper tidak tersedia.
- **Suara lebih natural: Google Translate TTS langsung** (tanpa
  dependency eksternal, langsung via `urllib`). Suara wanita Google
  bahasa Indonesia. Rantai baru: google → edge (GadisNeural) → android.

### Diubah
- `--engine auto` sekarang mencoba `google` dulu, lalu `edge`, lalu
  `android`. `--engine google` bisa dipakai untuk memaksa.
- Teks dibacain TTS dibersihkan dulu (`_tts_text`): markdown, emoji,
  dan simbol (`%`, `&`, `+`, ...) dihapus/diganti biar bacaan natural.
- Sitem prompt khusus selama panggilan suara minta AI jawab ringkas dan
  tidak mengulang/mencerminkan kata-kata user (anti gema).
- Default `--lang` sekarang `id-ID`.

## [2.4.0] - 2026-08-07

### Diubah
- **Voice chat sekarang suara cewe.** Jawaban disuarakan pakai edge-tts
  `id-ID-GadisNeural` (suara wanita Indonesia, neural, natural — kayak
  telponan beneran). Butuh `pip install edge-tts`. Kalau edge-tts gagal
  (offline/down), auto fallback ke TTS Android bahasa Indonesia
  (`termux-tts-speak -l id-ID`).
- `--voice-name <voice>` buat ganti suara edge-tts, `--engine edge|android`
  buat paksa mesin suara, `--rate` diteruskan ke edge-tts.

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
