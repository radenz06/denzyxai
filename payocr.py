"""payocr — OCR bukti pembayaran + parsing nominal otomatis.

Alur:
  1. OCR gambar (tesseract) → teks mentah.
  2. Text Parsing & Pattern Matching:
     - Regex nominal: /(?:Rp\\s?)?(\\d{1,3}(?:\\.\\d{3})*)/ dan
       /\\d{1,3}(?:\\.\\d{3})*/g
     - Keyword-based: baris yang mengandung "total", "jumlah", "bayar",
       "diterima", "transfer" diprioritaskan.
  3. Hasil: nominal terbaik (int) + teks OCR utuh (untuk audit owner).
"""

import json
import os
import re
import subprocess
import tempfile
from pathlib import Path

# regex utama sesuai spesifikasi
_RP_AMOUNT = re.compile(r"(?:Rp\s?)?(\d{1,3}(?:\.\d{3})*)", re.IGNORECASE)
_ALL_AMOUNT = re.compile(r"\d{1,3}(?:\.\d{3})*")

# kata kunci baris nominal, dikelompokkan berdasarkan kekuatan
# ("total bayar" paling kuat → baris itu PASTI berisi nominal uang).
_KEYWORD_GROUPS = (
    ("total", ("total bayar", "total pembayaran", "total transfer",
               "total", "jumlah")),
    ("bayar", ("diterima", "terbayar", "pembayaran", "bayar", "transfer",
               "nominal", "sebesar")),
)

# nominal transfer minimal yang wajar (hindari tanggal/jam/rekening)
_MIN_AMOUNT = 1000

_TESS = "tesseract"


def _tesseract_available():
    if os.environ.get("PAYOCR_NO_OCR"):
        return False
    return Path(_TESS).exists() or _which(_TESS)


def _which(name):
    for p in os.environ.get("PATH", "").split(os.pathsep):
        cand = Path(p) / name
        if cand.exists():
            return cand
    return None


def ocr_image(image_buffer, lang="eng"):
    """Ekstraksi teks dari gambar (image buffer) via tesseract.

    image_buffer: bytes gambar (jpg/png/dll).
    Return (text, err): text teks OCR, err pesan error bila gagal.
    """
    if not _tesseract_available():
        return "", "tesseract belum terpasang (apt install tesseract-ocr)"
    if not image_buffer:
        return "", "gambar kosong"
    with tempfile.NamedTemporaryFile(suffix=".png", delete=False) as tf:
        tf.write(image_buffer)
        tmp = tf.name
    try:
        try:
            from PIL import Image
            img = Image.open(tmp)
            if img.mode != "L":
                img = img.convert("L")
            img.save(tmp, "PNG")
        except Exception:  # noqa: BLE001
            pass  # tesseract tetap coba baca langsung
        proc = subprocess.run(
            [_TESS, tmp, "stdout", "-l", lang, "--psm", "6"],
            capture_output=True, timeout=45)
        if proc.returncode != 0:
            return "", (proc.stderr or b"").decode("utf-8", "replace")[:300]
        return proc.stdout.decode("utf-8", "replace"), None
    except subprocess.TimeoutExpired:
        return "", "OCR timeout"
    except Exception as e:  # noqa: BLE001
        return "", f"OCR error: {e}"
    finally:
        try:
            os.unlink(tmp)
        except OSError:
            pass


def _to_int(s):
    """'25.000' / '25,000' / '25 000' → 25000."""
    return int(re.sub(r"[^\d]", "", s))


def _amounts_in(text):
    """Semua kandidat nominal (int) dari teks."""
    out = []
    for m in _ALL_AMOUNT.finditer(text):
        s = m.group(0)
        try:
            a = _to_int(s)
        except ValueError:
            continue
        if a >= _MIN_AMOUNT:
            out.append((a, m.start()))
    return out


def _line_has_keyword(line, keywords):
    low = line.lower()
    return any(k in low for k in keywords)


def parse_amount(text, expected=None, tolerance=0.05):
    """Cari nominal terbaik dari teks OCR bukti pembayaran.

    Prioritas:
      1. Baris yang mengandung kata kunci (total/jumlah/bayar/dll).
      2. Kecocokan persis dengan harga (expected), toleransi ±5%.
      3. Nominal terbesar yang wajar (bukan no-rek / tanggal).

    Return dict: {amount, raw, line, exact} — amount None bila tak ketemu.
    """
    text = text or ""
    lines = [ln.strip() for ln in text.splitlines() if ln.strip()]
    all_cands = _amounts_in(text)

    def pick(cands, label):
        if not cands:
            return None
        best = max(cands, key=lambda c: c[0])
        # ambil potongan teks di sekitar match
        start = max(0, text.rfind("\n", 0, best[1]))
        end = text.find("\n", best[1])
        raw_line = text[start:end].strip()
        exact = bool(expected and abs(best[0] - expected) <= expected * tolerance)
        return {"amount": best[0], "raw": best[0], "line": raw_line,
                "label": label, "exact": exact}

    # 1) baris berkeyword — "total bayar" (group total) lebih kuat dari yang lain.
    #    Baris keyword PASTI berisi nominal uang yang dikirim (per logika).
    for _, keywords in _KEYWORD_GROUPS:
        for ln in lines:
            if _line_has_keyword(ln, keywords):
                c = [(a, i) for a, i in _amounts_in(ln)]
                if c:
                    r = pick(c, "keyword")
                    r["line"] = ln
                    return r

    # 2) cocok expected
    if expected:
        for a, i in all_cands:
            if abs(a - expected) <= expected * tolerance:
                start = max(0, text.rfind("\n", 0, i))
                end = text.find("\n", i)
                return {"amount": a, "raw": a, "line": text[start:end].strip(),
                        "label": "exact", "exact": True}
    # 3) nominal terbesar (hindari tanggal seperti 25.000.000?)
    if all_cands:
        r = pick(all_cands, "terbesar")
        # filter: kalau cuma 1-3 digit mungkin bukan nominal transfer
        if len(str(r["amount"])) >= 4:
            return r
    return None


def verify_payment(text, expected, tolerance=0.05):
    """Rangkuman hasil verifikasi bukti untuk laporan ke owner."""
    res = parse_amount(text, expected=expected, tolerance=tolerance)
    amount = (res or {}).get("amount")
    shortage = None
    if amount is not None and expected:
        shortage = max(0, expected - amount)
    return {
        "ok": bool(res and expected and amount >= expected),
        "amount": amount,
        "expected": expected,
        "shortage": shortage,
        "raw": (res or {}).get("raw"),
        "line": (res or {}).get("line"),
        "exact": bool((res or {}).get("exact")),
        "text": text,
    }
