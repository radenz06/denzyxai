# Contributing

Makasih udah mau ikut nyempetin repo ini. Aturannya simpel:

## Laporan bug / ide

- Buka [issue](https://github.com/radenz06/denzyxai/issues) dengan judul yang
  jelas dan langkah reproduksi (kalau bug).
- Jangan lampirkan token/key.

## Pull request

1. Fork repo, kerja di branch sendiri (`git checkout -b fix/nama-issue`).
2. Pastikan test jalan: `python3 -m pytest -q tests/`.
3. Jangan commit file lokal: `sessions/`, `webconfig.json`, `webdata/`,
   `.env`, `system_prompt.md`.
4. Bikin PR ke branch `main`, jelasin apa dan kenapa.

## Standar kode

- Python 3.8+, stdlib only untuk file inti.
- Komentar secukupnya.
- Tool baru di `dscli.py`: tambah schema di `TOOLS` + implementasi di
  `TOOL_IMPL`.
