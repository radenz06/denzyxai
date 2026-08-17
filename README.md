# denzyx AI

<p align="center">
  <img src="docs/logo.jpg" width="120" alt="denzyx AI">
</p>

<p align="center">
  <b>Denzyx AI</b>
</p>

<p align="center">
  <a href="https://github.com/radenz06/denzyxai"><img src="https://img.shields.io/badge/GitHub-repo-181717?style=for-the-badge&logo=github&logoColor=white" alt="GitHub"></a>
  <img src="https://img.shields.io/badge/Status-Active-22c55e?style=for-the-badge" alt="Status">
</p>

> Modular AI ecosystem: TUI chat, web member area, Telegram bot, auto-reply 24 jam.

---

## 📦 Install

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

## 📖 Cara Pakai

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

Fitur: registrasi + login member, chat AI streaming, panel model AI sendiri
(status langganan, bayar via QR).

### Panel owner & daemon
```sh
python3 admin-denz.py            # kelola member, setup, restart
python3 auto-denz.py install     # auto-reply 24 jam
python3 denzbot.py               # bot Telegram
```

---

## 🛡️ Keamanan

- Web chat memakai **system prompt khusus**: user yang minta akses server/terminal ditolak (`ACCESS DENIED`) — web hanya untuk ngobrol biasa
- Tool pentest hanya berjalan **legal & berizin** (target milik sendiri / kontrak), dengan cleanup otomatis
- Config, password, dan data member **terenkripsi** (Fernet), tidak ada data di cloud
- WAF bawaan memblokir IP; semua form pakai CSRF token

---

## 🤝 Kontribusi

Pull request dipersilakan. Untuk perubahan besar, buka issue dulu.

---

## 📜 Lisensi

MIT — lihat [LICENSE](LICENSE).

<p align="center">
  <sub>dibuat oleh <a href="https://github.com/radenz06">radenz06</a></sub>
</p>