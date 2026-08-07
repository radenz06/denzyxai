# Contributing

Makasih udah mau ikut nyempetin repo ini. Aturannya simpel:

## Laporan bug / ide

- Buka [issue](https://github.com/radenz06/denzyz/issues) dengan judul yang
  jelas dan langkah reproduksi (kalau bug).
- Sertakan output `denzyx.py` atau log daemon (`~/.denzyx_auto.log`) yang
  relevan. Jangan lampirkan token/key.

## Pull request

1. Fork repo, kerja di branch sendiri (`git checkout -b fix/nama-issue`).
2. Pastikan `make check` lolos (kompilasi + import) dan test jalan:
   `python3 -m pytest -q tests/`.
3. Jangan commit file lokal: `sessions/`, `*.log`, `*.pid`, `.env`,
   `local.properties`.
4. Bikin PR ke branch `main`, jelasin apa dan kenapa.

## Standar kode

- Python 3.8+, tanpa dependency luar untuk file inti (`denzyx.py`,
  `dscli.py`, `auto-denz.py`). Cuma pakai stdlib.
- Komentar secukupnya, bahasa Indonesia kalau njelasin "kenapa", bukan
  cuma mengulang kode.
- Tool baru di `dscli.py`: tambah schema di `TOOLS` + implementasi di
  `TOOL_IMPL`, terus update README tabel tool.
