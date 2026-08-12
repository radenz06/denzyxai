# Security — denzyx web hosting (cloudflared)

Lapisan keamanan yang aktif di server web `webdenz.py` + `waf.py`.

## Perimeter

- **Akses via cloudflared** — quick tunnel. Server default bind ke
  `127.0.0.1` (cuma tunnel yang bisa menjangkau). Kalau `host` masih
  `0.0.0.0` di `webconfig.json`, segera ubah ke `127.0.0.1`.
- **IP asli klien** diambil dari `CF-Connecting-IP` / `X-Forwarded-For`,
  dan header itu **hanya dipercaya bila koneksi datang dari loopback**
  (jadi spoof header dari akses langsung tidak berpengaruh). Lihat
  `waf.get_real_ip()`.

## WAF (Web Application Firewall) — `waf.py`

| Serangan | Deteksi | Aksi |
| --- | --- | --- |
| Alat peretas (Burp, sqlmap, nikto, nmap, dll) | User-Agent | 403 + ban permanen |
| Scan path populer (wp-login.php, phpmyadmin, .git, dll) | honeypot path | 403 + ban permanen |
| Path traversal (`../`, `%2e%2e`) | pola URL | 403 + ban permanen |
| Injection (SQLi, XSS, LFI) | pola query/path | 403 + ban permanen |
| Endpoint scan (banyak 404 ke path acak) | 25 path/60 dtk | ban permanen |
| Brute-force login/owner/register | gagal login + rate-limit ≥ 6/600 dtk | ban permanen |
| Slowloris / koneksi gantung | socket timeout 20 dtk | tutup koneksi |
| Connection flood | ≤ 8 koneksi paralel/IP | 429 |
| Crawl/bot | ≤ 600 request/menit/IP | 429 |
| Request smuggling | tolak `Host` berisi CR/LF | 400/403 |

IP yang di-ban **tidak bisa akses apa pun** (seluruh path → 403), disimpan
permanen di `webdata/bans.json` (gitignored), dan pemilik dapat
**notifikasi Telegram** berisi IP + lokasi geografis + UA + path + waktu.

IP loopback (`127.0.0.1`, `::1`) tidak pernah di-ban otomatis.

### Pelacak pengunjung (`track.py`)

Setiap request ke server terekam di `webdata/visitors.json` + riwayat di
`webdata/logs/visitors.log` (1 baris/menit/IP). Data yang diambil:
- **IP public & private**: peer socket, `CF-Connecting-IP`, rantai
  `X-Forwarded-For`/`X-Real-IP` (header hanya dipercaya dari koneksi
  loopback — sama dengan `waf.get_real_ip`, cegah spoof), diklasifikasikan
  public/private/loopback.
- **Lokasi & ISP**: geolokasi IP publik via ipwho.is (async + cache — tidak
  memperlambat request; IP private/loopback tidak di-lookup).
- **Software**: User-Agent di-parse → browser, OS, device, engine; bot dan
  scanner (sqlmap, nikto, nmap, curl, wget, dll) ditandai.
- **Perilaku**: path, metode, kode status response, referer, waktu.

Kelola: owner panel `/owner/visitors` (daftar, detail, ⛔ Ban, hapus data),
CLI `admin-denz.py visitors [ip|cari]` / `visitors-clear`. Matikan dengan
`track_visitors: false` (atau `track_geo: false` untuk geolokasi saja).
Data pengunjung dihapus lewat tombol "Hapus semua data pengunjung".

### Kelola ban

- Owner panel web: `/owner/security` (daftar + unban).
- Bot Telegram: `/bans`, `/unbanip <ip>`, `/block <ip>`.
- Terminal: `python3 admin-denz.py bans | unban <ip> | block <ip>`.

## Aplikasi web

- **CSRF**: token HMAC per-sesi (cookie `HttpOnly`) di semua form POST.
- **Rate limit**: login/register/owner (8×/10 mnt), chat (30×/mnt).
- **Cookie**: `HttpOnly; SameSite=Lax`, `Secure` otomatis saat lewat tunnel
  edge TLS (HTTPS) → HSTS juga aktif.
- **Header keamanan**: `X-Content-Type-Options`, `X-Frame-Options`,
  `Referrer-Policy`, `Cross-Origin-Opener-Policy`, CSP, `Permissions-Policy`.
- **CORS**: bisa dibatasi via `cors_origins` di `webconfig.json` (isi domain
  frontend Vercel + tunnel). Kosong = `*` (default, agar frontend Vercel
  tetap jalan).
- **Body size**: dibatasi 256 KB (`max_body`).
- **Enkripsi data**: password member & owner pakai PBKDF2-HMAC-SHA256 + salt
  (owner) / Fernet (member); **`webconfig.json` disimpan TERENKRIPSI di disk**
  (Fernet, key di `webdata/.config.key`, chmod 600 — lihat `securecfg.py`),
  config plaintext lama otomatis dimigrasi; `webconfig.json`, `webdata/`,
  `sessions/` semuanya gitignored — tidak ada rahasia terbaca di repo GitHub.
- **Halaman blokir**: IP yang di-ban dapat halaman 403 berisi marquee
  "KAMU BODOH BANGET SIH, JANGAN GITU YA LAIN KALI😹🖕" (pesan dari denzyx).

## TLS (saat pakai `ssl_cert`/`ssl_key` di `webconfig.json`)

- **TLS 1.0 / 1.1 dimatikan** (`minimum_version = TLS 1.2`).
- **Cipher suite lemah diblokir**: RC4, DES, 3DES, MD5, CBC, SHA-1, NULL,
  export, PSK/SRP/DSS, LOW, CAMELLIA, SEED, IDEA — hanya cipher modern
  ECDHE+AESGCM / ECDHE+CHACHA20 / DHE+AESGCM yang diizinkan.
- **Server-preference cipher** + **kompresi TLS dimatikan** (anti CRIME).
- Catatan: akses publik via tunnel cloudflared TLS-nya ditangani Cloudflare
  (edge selalu TLS 1.2/1.3 modern); pengaturan di atas mengamankan akses
  HTTPS langsung ke server bila dipakai.

## Konfigurasi relevan (`webconfig.json`)

```jsonc
"host": "127.0.0.1",          // jangan 0.0.0.0 di luar
"waf": true,
"ban_scan_threshold": 25,     // 404 acak per 60 dtk → ban
"ban_fail_threshold": 6,      // gagal login/rate-limit per 600 dtk → ban
"max_body": 262144,           // bytes
"req_rate_max": 600,          // per menit per IP
"conn_max": 8,                // koneksi paralel per IP
"cors_origins": [],           // allowlist origin CORS (kosong = *)
"allowed_hosts": [],          // allowlist Host (kosong = tak diperketat)
"tg_notify_security": true,   // notifikasi TG saat ban
"track_visitors": true,       // rekam pengunjung (IP/lokasi/software)
"track_geo": true             // geolokasi via ipwho.is (async)
```

## Melapor / berkontribusi

Temukan celah? Buka issue atau hubungi owner lewat Telegram. Jangan commit
`webconfig.json` / `webdata/` — file itu sudah masuk `.gitignore`.
