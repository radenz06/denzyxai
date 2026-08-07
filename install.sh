#!/usr/bin/env sh
# install.sh — setup sekali jalan untuk denzyz AI (Termux/Linux).
# Idempotent: aman dijalanin ulang.

set -e

# --- cari folder app (script ini) ---
APP_DIR=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)

# --- cek python ---
if [ -x /data/data/com.termux/files/usr/bin/python3 ]; then
    PY=/data/data/com.termux/files/usr/bin/python3
else
    PY=python3
fi
if ! command -v "$PY" >/dev/null 2>&1; then
    echo "[denzyz] python3 tidak ketemu. Install dulu:  pkg install python3"
    exit 1
fi

# --- pastikan file inti ada ---
for f in denzyx.py dscli.py system_prompt.md; do
    [ -f "$APP_DIR/$f" ] || { echo "[denzyz] $f hilang di $APP_DIR"; exit 1; }
done

# --- launcher executable ---
chmod +x "$APP_DIR/denzyx" 2>/dev/null || true

# --- cek kompilasi ---
echo "[denzyz] cek sintaks..."
"$PY" -m py_compile "$APP_DIR/denzyx.py" "$APP_DIR/dscli.py" \
    "$APP_DIR/auto-denz.py" 2>/dev/null || true

# --- folder sesi ---
mkdir -p "$APP_DIR/sessions"

# --- shortcut shell (opsional) ---
PROFILE=""
for p in "$HOME/.bashrc" "$HOME/.zshrc"; do
    [ -f "$p" ] && PROFILE="$p" && break
done
if [ -n "$PROFILE" ] && ! grep -q "denzyx ai" "$PROFILE" 2>/dev/null; then
    {
        echo ""
        echo "# denzyz AI launcher (dari install.sh)"
        echo "denzyx() { cd \"$APP_DIR\" && ./denzyx \"\$@\"; }"
    } >> "$PROFILE"
    echo "[denzyz] shortcut 'denzyx' ditambahkan ke $PROFILE"
else
    echo "[denzyz] shortcut 'denzyx' sudah ada / profile tidak ditemukan."
fi

echo ""
echo "[denzyz] selesai. Cara pakai:"
echo "  1. ./denzyx              -> buka TUI"
echo "  2. python3 auto-denz.py install   -> auto-reply 24 jam"
echo "  3. pkg install termux-api         -> fitur perangkat (HP)"
