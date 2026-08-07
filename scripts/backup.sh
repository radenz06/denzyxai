#!/usr/bin/env sh
# backup.sh — arsipkan sesi, konfigurasi, dan log daemon ke storage.
# Default: ~/storage/shared/denzyz-backup/ (Termux), atau ./backup/.

set -e

APP_DIR=$(CDPATH= cd -- "$(dirname -- "$0")/.." && pwd)
STAMP=$(date +%Y%m%d-%H%M%S)

# cari folder tujuan
if [ -d "$HOME/storage/shared" ]; then
    DEST="$HOME/storage/shared/denzyz-backup"
else
    DEST="$APP_DIR/backup"
fi
mkdir -p "$DEST"

ARCHIVE="$DEST/denzyz-$STAMP.tar.gz"
tar -czf "$ARCHIVE" \
    -C "$APP_DIR" \
    sessions system_prompt.md theme.md \
    --exclude='__pycache__' 2>/dev/null || true

# log daemon (kalau ada, di luar folder app)
LOG=/data/data/com.termux/files/home/.denzyx_auto.log
[ -f "$LOG" ] && cp "$LOG" "$DEST/auto-$STAMP.log" 2>/dev/null || true

echo "[denzyz] backup selesai:"
ls -lh "$ARCHIVE" "$DEST"/auto-*.log 2>/dev/null || true
echo ""
echo "Ambil ke PC:  scp $ARCHIVE user@host:~/"
