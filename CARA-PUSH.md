# Cara Push ke GitHub

Commit terakhir siap di-push: `0f643c7` (v2.1.0).
Pilih salah satu cara di bawah. Ganti `<TOKEN>` sama PAT kamu (izin `repo`).

## Cara 1 — git minta password (paling aman, token nggak kesimpen)

```sh
cd "/data/data/com.termux/files/home/denzyx ai"
git push origin main
# Username: radenz06
# Password: <TOKEN>   <- tempel PAT, bukan password GitHub biasa
```

## Cara 2 — sekali push (token cuma dipakai saat itu juga)

```sh
cd "/data/data/com.termux/files/home/denzyx ai"
git -c credential.helper= push https://radenz06:<TOKEN>@github.com/radenz06/denzyz.git main
```

`-c credential.helper=` memastikan token TIDAK tersimpan ke disk.

## Cara 3 — simpan biar nggak nanya lagi

```sh
cd "/data/data/com.termux/files/home/denzyx ai"
git config credential.helper store
git push https://radenz06:<TOKEN>@github.com/radenz06/denzyz.git main
# token bakal kesimpen di ~/.git-credentials
```

## Cek hasil

```sh
cd "/data/data/com.termux/files/home/denzyx ai"
git log --oneline -3
git status
```
