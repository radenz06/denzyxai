# Keamanan

Laporan tentang celah keamanan: buka issue atau hubungi owner lewat Telegram.

## Catatan untuk pengguna

- Akses publik lewat tunnel cloudflared; server sebaiknya bind di
  `127.0.0.1`.
- Data member (`webconfig.json`, `webdata/`, `sessions/`) tidak ikut
  di-commit (sudah di `.gitignore`).
- Password member dan owner disimpan terenkripsi, tidak pernah plaintext.
- Jangan commit file konfigurasi lokal ke repo publik.
