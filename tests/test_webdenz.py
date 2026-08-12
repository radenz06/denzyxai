# -*- coding: utf-8 -*-
"""Test webdenz — member/admin system (isolasi: tmp dir + env var)."""

import importlib
import json
import os
import threading
import urllib.parse
import urllib.request
from datetime import datetime, timedelta, timezone

import pytest


@pytest.fixture(scope="function", autouse=True)
def wd(tmp_path):
    """Isolasi webdenz ke tmp dir per-test (env var + reload modul)."""
    os.environ["WEBDENZ_CONFIG"] = str(tmp_path / "webconfig.json")
    os.environ["WEBDENZ_DATA"] = str(tmp_path / "webdata")
    os.environ["WEBDENZ_TRACK_GEO"] = "0"  # no geolokasi network di test
    import track
    importlib.reload(track)
    track.set_geo(False)
    import webdenz
    importlib.reload(webdenz)
    webdenz._mkdirs()
    return webdenz


@pytest.fixture(autouse=True)
def fresh_cfg(wd):
    import webdenz
    cfg = webdenz.load_config()
    cfg["secret"] = "test-secret"
    cfg["owner"] = {"username": "denzyx",
                    "password_hash": webdenz.hash_password("ownerpw", "s1"),
                    "salt": "s1"}
    cfg["tg_bot_token"] = ""
    cfg["tg_chat_id"] = ""
    webdenz.save_config(cfg)
    return cfg


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def _no_redirect_opener():
    return urllib.request.build_opener(_NoRedirect)


def _mock_stream_chat(wd, monkeypatch):
    """Ganti denzyx.stream_chat dengan balasan tetap (tanpa network)."""
    def fake_stream_chat(state, prompt, q):
        state.messages.append({"role": "user", "content": prompt})
        state.messages.append({"role": "assistant", "content": "balasan AI"})
        q.put(("content", "balasan AI"))
        q.put(("done", None))
    import denzyx
    monkeypatch.setattr(denzyx, "stream_chat", fake_stream_chat)


class TestCrypto:
    def test_hash_verify(self, wd):
        import webdenz
        d = webdenz.hash_password("pw", "salt")
        assert webdenz.verify_password("pw", "salt", d)
        assert not webdenz.verify_password("pw2", "salt", d)
        assert not webdenz.verify_password("pw", "salt2", d)

    def test_enc_dec(self, wd):
        import webdenz
        tok = webdenz.enc_secret("rahasia123")
        assert tok != "rahasia123"
        assert webdenz.dec_secret(tok) == "rahasia123"


class TestMemberStore:
    def test_create_load_status(self, wd):
        import webdenz
        webdenz.create_member("budi", "pw1", "Budi", "1.2.3.4")
        m = webdenz.load_member("budi")
        assert m["username"] == "budi"
        assert m["status"] == "pending"
        assert webdenz.member_status(m) == "pending"
        # decrypt password
        assert webdenz.dec_secret(m["password"]) == "pw1"

    def test_activate_expire(self, wd):
        import webdenz
        from datetime import datetime, timedelta
        webdenz.create_member("siti", "pw1", "Siti")
        m = webdenz.load_member("siti")
        m["status"] = "active"
        m["expires_at"] = (datetime.now() + timedelta(days=30)).isoformat()
        webdenz.save_member(m)
        assert webdenz.member_status(webdenz.load_member("siti")) == "active"
        m["expires_at"] = (datetime.now() - timedelta(days=1)).isoformat()
        webdenz.save_member(m)
        assert webdenz.member_status(webdenz.load_member("siti")) == "expired"

    def test_ban_overrides(self, wd):
        import webdenz
        from datetime import datetime, timedelta
        webdenz.create_member("joko", "pw1")
        m = webdenz.load_member("joko")
        m["status"] = "active"
        m["expires_at"] = (datetime.now() + timedelta(days=30)).isoformat()
        webdenz.save_member(m)
        assert webdenz.member_status(m) == "active"
        m["status"] = "banned"
        webdenz.save_member(m)
        assert webdenz.member_status(m) == "banned"

    def test_sessions_md(self, wd):
        import webdenz
        webdenz.create_member("ani", "pw1")
        tok = webdenz.issue_member_session("ani", "9.9.9.9")
        m, sess = webdenz.member_by_token(tok)
        assert m["username"] == "ani"
        assert sess["ip"] == "9.9.9.9"
        assert webdenz.session_md_path("ani").exists()

    def test_append_chat_writes(self, wd):
        import webdenz
        webdenz.create_member("rudi", "pw1")
        webdenz.append_chat("rudi", "halo", "hai juga")
        md = webdenz.session_md_path("rudi").read_text(encoding="utf-8")
        assert "halo" in md and "hai juga" in md
        m = webdenz.load_member("rudi")
        assert len(m["messages"]) == 2

    def test_register_log_has_password(self, wd):
        import webdenz
        webdenz.create_member("eko", "secretpw", "Eko", "7.7.7.7")
        rows = webdenz.read_log("register")
        assert any("secretpw" in r for r in rows)

    def test_list_members(self, wd):
        import webdenz
        webdenz.create_member("a1", "p")
        webdenz.create_member("a2", "p")
        assert len(webdenz.list_members()) == 2

    def test_owner_token(self, wd):
        import webdenz
        tok = webdenz.issue_owner_token()
        assert webdenz.owner_token_valid(tok)
        assert not webdenz.owner_token_valid("nope")


class TestSecureConfig:
    def test_config_encrypted_on_disk(self, wd, fresh_cfg):
        """webconfig.json di-disk terenkripsi: rahasia tidak terbaca mentah."""
        import webdenz
        raw = webdenz.CONFIG_PATH.read_text(encoding="utf-8")
        assert not raw.lstrip().startswith("{")
        assert "ownerpw" not in raw and "test-secret" not in raw
        # bisa dibuka lagi lewat load_config & securecfg
        assert webdenz.load_config()["secret"] == "test-secret"
        cfg = webdenz.securecfg.read(webdenz.CONFIG_PATH, webdenz.DATA_DIR)
        assert cfg["secret"] == "test-secret"

    def test_config_legacy_plaintext_migrate(self, wd):
        """Config plaintext lama otomatis dibaca lalu di-migrasi ke enkripsi."""
        import webdenz
        webdenz.CONFIG_PATH.write_text(
            json.dumps({"tg_chat_id": "111", "owner": {"username": "denzyx"}}),
            encoding="utf-8")
        cfg = webdenz.load_config()
        assert cfg["tg_chat_id"] == "111"
        assert cfg["owner"]["username"] == "denzyx"
        assert cfg["secret"]  # default secret dibuat
        raw = webdenz.CONFIG_PATH.read_text(encoding="utf-8")
        assert not raw.lstrip().startswith("{")

    def test_missing_config_creates_encrypted(self, wd):
        import webdenz
        cfg = webdenz.load_config()
        assert cfg["secret"]
        raw = webdenz.CONFIG_PATH.read_text(encoding="utf-8")
        assert not raw.lstrip().startswith("{")

    def test_unreadable_config_not_overwritten(self, wd):
        """Regression: config yang ada tapi gagal decrypt TIDAK boleh
        ditimpa default + secret baru (nilai asli hilang)."""
        import base64
        import hashlib
        import webdenz
        webdenz.save_config({"secret": "asli", "tg_bot_token": "token-asli"})
        asli = webdenz.CONFIG_PATH.read_bytes()
        # ubah key → read() gagal decrypt
        (webdenz.DATA_DIR / ".config.key").write_bytes(
            base64.urlsafe_b64encode(hashlib.sha256(b"beda").digest()))
        cfg = webdenz.load_config()
        assert cfg["secret"] != "asli"  # default in-memory (tanpa save)
        # file asli tidak boleh berubah
        assert webdenz.CONFIG_PATH.read_bytes() == asli

    def test_setpass_unreadable_config_aborts(self, wd, monkeypatch):
        """Regression: lic.setpass() tak boleh menghapus isi config lain
        saat webconfig.json tidak terbaca (hanya menulis {lic})."""
        import webdenz
        webdenz.save_config({"secret": "asli", "tg_bot_token": "token-asli"})
        asli = webdenz.CONFIG_PATH.read_bytes()

        import lic
        class _Stdin:
            @staticmethod
            def isatty():
                return True
        monkeypatch.setattr(lic.sys, "stdin", _Stdin())
        monkeypatch.setattr(lic, "verify", lambda pw: True)
        monkeypatch.setattr(lic.securecfg, "read", lambda *a, **k: None)
        seq = iter(["lama", "baru123", "baru123"])
        monkeypatch.setattr(lic.getpass, "getpass", lambda prompt="": next(seq))

        assert lic.setpass() == 1  # gagal, config tidak disentuh
        assert webdenz.CONFIG_PATH.read_bytes() == asli

    def test_blocked_page_marquee(self, wd):
        import webdenz
        html = webdenz._blocked_page("endpoint scan")
        assert "<marquee" in html
        assert "KAMU BODOH BANGET SIH, JANGAN GITU YA LAIN KALI" in html
        assert "pesan dari denzyx" in html
        assert "403" in html

    def test_error_page_marquee_all_codes(self, wd):
        import webdenz
        for code, title in ((403, "Akses Diblokir"),
                            (404, "Halaman Tidak Ada"),
                            (405, "Metode Tidak Diizinkan")):
            html = webdenz._error_page(code, title, "coba-coba")
            assert "KAMU BODOH BANGET SIH, JANGAN GITU YA LAIN KALI" in html
            assert "<marquee" in html
            assert str(code) in html and title in html
            assert "pesan dari denzyx" in html

    def test_tls_ciphers_hardening(self, wd, tmp_path):
        """TLS 1.2+ & cipher lemah diblokir (vuln Weak Cipher Suites)."""
        import ssl
        import webdenz
        from cryptography import x509
        from cryptography.hazmat.primitives import hashes, serialization
        from cryptography.hazmat.primitives.asymmetric import rsa

        k = rsa.generate_private_key(public_exponent=65537, key_size=2048)
        name = x509.Name([x509.NameAttribute(x509.NameOID.COMMON_NAME, "test")])
        cert = (x509.CertificateBuilder()
                .subject_name(name).issuer_name(name)
                .public_key(k.public_key())
                .serial_number(x509.random_serial_number())
                .not_valid_before(
                    datetime.now(timezone.utc) - timedelta(days=1))
                .not_valid_after(
                    datetime.now(timezone.utc) + timedelta(days=1))
                .sign(k, hashes.SHA256()))
        cert_f = tmp_path / "t.crt"
        key_f = tmp_path / "t.key"
        cert_f.write_bytes(cert.public_bytes(serialization.Encoding.PEM))
        key_f.write_bytes(k.private_bytes(
            serialization.Encoding.PEM,
            serialization.PrivateFormat.PKCS8,
            serialization.NoEncryption()))

        ctx = webdenz._ssl_context(str(cert_f), str(key_f))
        assert ctx.minimum_version == ssl.TLSVersion.TLSv1_2
        assert ctx.options & ssl.OP_NO_COMPRESSION
        names = [c["name"] for c in ctx.get_ciphers()]
        assert names
        for n in names:
            assert not any(w in n for w in ("RC4", "3DES", "DES-CBC", "MD5"))
            assert "GCM" in n or "CHACHA20" in n or "SHA384" in n


class TestMemberChat:
    def test_member_chat(self, wd, monkeypatch):
        import webdenz
        _mock_stream_chat(wd, monkeypatch)
        webdenz.create_member("dono", "pw1")
        reply, err = webdenz.member_chat("dono", "tes")
        assert err is None
        assert reply == "balasan AI"
        m = webdenz.load_member("dono")
        assert len(m["messages"]) == 2


class TestPages:
    def test_login_page(self, wd):
        import webdenz
        html = webdenz._login_page(webdenz.load_config())
        assert "denzyx" in html.lower() and "password" in html.lower()

    def test_owner_page(self, wd):
        import webdenz
        html = webdenz._owner_page(webdenz.load_config())
        assert "Owner Panel" in html

    def test_pending_page_has_pay_button(self, wd):
        import webdenz
        cfg = webdenz.load_config()
        cfg["tg_owner_username"] = "colipopi"
        webdenz.save_config(cfg)
        webdenz.create_member("budi", "pw123", "Budi")
        html = webdenz._status_page(webdenz.load_member("budi"))
        assert "Konfirmasi Aktivasi ke Telegram" in html
        assert "https://t.me/colipopi?text=" in html
        assert "aktivasi akun & konfirmasi pembayaran" in urllib.parse.unquote(
            webdenz._pay_tg_link(cfg, webdenz.load_member("budi")))
        assert "/qr" in html
        # aktif → tombol hilang
        m = webdenz.load_member("budi")
        m["status"] = "active"
        from datetime import datetime, timedelta
        m["expires_at"] = (datetime.now() + timedelta(days=30)).isoformat()
        webdenz.save_member(m)
        html2 = webdenz._status_page(webdenz.load_member("budi"))
        assert "Konfirmasi Aktivasi ke Telegram" not in html2


class TestHTTP:
    def _start(self, wd):
        import webdenz
        srv = webdenz.ThreadingHTTPServer(("127.0.0.1", 0), webdenz.Handler)
        t = threading.Thread(target=srv.serve_forever, daemon=True)
        t.start()
        return srv, t

    def _post(self, port, path, data=None, cookie="", extra_headers=None):
        import urllib.request
        body = urllib.parse.urlencode(data or {}).encode()
        hdrs = {"Cookie": cookie}
        hdrs.update(extra_headers or {})
        req = urllib.request.Request(
            f"http://127.0.0.1:{port}{path}", data=body, method="POST",
            headers=hdrs)
        opener = _no_redirect_opener()
        try:
            with opener.open(req, timeout=10) as r:
                return r.status, r.headers, r.read().decode()
        except urllib.error.HTTPError as e:
            return e.code, e.headers, e.read().decode()

    def _get(self, port, path, cookie="", extra_headers=None):
        import urllib.request
        hdrs = {"Cookie": cookie}
        hdrs.update(extra_headers or {})
        req = urllib.request.Request(
            f"http://127.0.0.1:{port}{path}", headers=hdrs)
        opener = _no_redirect_opener()
        try:
            with opener.open(req, timeout=10) as r:
                return r.status, r.headers, r.read().decode()
        except urllib.error.HTTPError as e:
            return e.code, e.headers, e.read().decode()

    def _cookie_val(self, setcookie, name):
        for part in str(setcookie).split(","):
            part = part.strip()
            if part.startswith(name + "="):
                return part.split("=")[1].split(";")[0]
        return ""

    def _csrf_from(self, body):
        import re
        mt = re.search(r'name="_csrf" value="([^"]+)"', body)
        return mt.group(1) if mt else None

    def _csrf(self, port, path, cookie=""):
        """GET halaman, ambil pasangan (cookie denz_csrf, token _csrf)."""
        import webdenz
        st, h, body = self._get(port, path, cookie)
        assert st == 200
        raw = self._cookie_val(h.get("Set-Cookie", ""), "denz_csrf")
        tok = self._csrf_from(body)
        assert raw and tok
        joined = (cookie + "; " if cookie else "") + f"denz_csrf={raw}"
        return joined, tok

    def test_register_login_chat(self, wd, monkeypatch):
        import webdenz
        _mock_stream_chat(wd, monkeypatch)
        srv, t = self._start(wd)
        port = srv.server_address[1]
        try:
            cookie, tok = self._csrf(port, "/register")
            # register
            st, h, body = self._post(port, "/register",
                                     {"username": "newbie", "display_name": "Newbie",
                                      "password": "pw123", "_csrf": tok}, cookie)
            assert st == 200 and "pending" in body
            # login sebelum aktif → ditolak
            st, h, body = self._post(port, "/login",
                                     {"username": "newbie", "password": "pw123",
                                      "_csrf": tok}, cookie)
            assert "pending" in body
            # activate
            m = webdenz.load_member("newbie")
            from datetime import datetime, timedelta
            m["status"] = "active"
            m["expires_at"] = (datetime.now() + timedelta(days=30)).isoformat()
            webdenz.save_member(m)
            # login sukses → redirect + cookie
            cookie, tok = self._csrf(port, "/login")
            st, h, body = self._post(port, "/login",
                                     {"username": "newbie", "password": "pw123",
                                      "_csrf": tok}, cookie)
            assert st in (302, 303)
            mcookie = h.get("Set-Cookie", "")
            assert "denz_member=" in mcookie
            mtok = self._cookie_val(mcookie, "denz_member")
            # chat page
            st, h, body = self._get(port, "/chat", f"denz_member={mtok}")
            assert st == 200
            # api chat
            st, h, body = self._post(port, "/api/chat",
                                     {"message": "halo"}, f"denz_member={mtok}")
            assert st == 200
            j = json.loads(body)
            assert j.get("reply") == "balasan AI"
            assert webdenz.load_member("newbie")["login_count"] >= 1
            # streaming chat
            st, h, body = self._post(port, "/api/chat/stream",
                                     {"message": "halo stream"},
                                     f"denz_member={mtok}")
            assert st == 200
            assert '"t": "text"' in body and '"t": "done"' in body
            # status page member
            st, h, body = self._get(port, "/status", f"denz_member={mtok}")
            assert st == 200 and "newbie" in body
            # security headers
            assert "nosniff" in h.get("X-Content-Type-Options", "")
        finally:
            srv.shutdown()
            t.join(timeout=3)

    def test_owner_login_and_member_page(self, wd):
        import webdenz
        srv, t = self._start(wd)
        port = srv.server_address[1]
        webdenz.create_member("citra", "pwcitra", "Citra")
        try:
            cookie, tok = self._csrf(port, "/owner")
            # owner login
            st, h, body = self._post(port, "/owner/login",
                                     {"username": "denzyx", "password": "ownerpw",
                                      "_csrf": tok}, cookie)
            assert st in (302, 303)
            ocookie = h.get("Set-Cookie", "")
            otok = self._cookie_val(ocookie, "denz_owner")
            assert otok
            # owner page
            st, h, body = self._get(port, "/owner", f"denz_owner={otok}")
            assert st == 200 and "citra" in body
            # member detail page — password terlihat oleh owner
            st, h, body = self._get(port, "/owner/member/citra",
                                    f"denz_owner={otok}")
            assert st == 200 and "pwcitra" in body
            # reset password via owner
            cookie, tok = self._csrf(port, "/owner/member/citra")
            st, h, body = self._post(port, "/owner/member/citra",
                                     {"action": "resetpass", "new_password": "baru456",
                                      "_csrf": tok},
                                     f"denz_owner={otok}; {cookie}")
            assert st in (302, 303)
            assert webdenz.dec_secret(webdenz.load_member("citra")["password"]) == "baru456"
        finally:
            srv.shutdown()
            t.join(timeout=3)

    def test_csrf_required(self, wd):
        import webdenz
        srv, t = self._start(wd)
        port = srv.server_address[1]
        try:
            st, h, body = self._post(port, "/login",
                                     {"username": "x", "password": "y"})
            assert st == 403 and "CSRF" in body
            assert "KAMU BODOH BANGET SIH" in body
            st, h, body = self._post(port, "/owner/login",
                                     {"username": "denzyx", "password": "ownerpw"})
            assert st == 403 and "CSRF" in body
        finally:
            srv.shutdown()
            t.join(timeout=3)

    def test_change_password(self, wd):
        import webdenz
        from datetime import datetime, timedelta
        srv, t = self._start(wd)
        port = srv.server_address[1]
        webdenz.create_member("ganti", "lama123", "Ganti")
        m = webdenz.load_member("ganti")
        m["status"] = "active"
        m["expires_at"] = (datetime.now() + timedelta(days=30)).isoformat()
        webdenz.save_member(m)
        tok = webdenz.issue_member_session("ganti", "9.9.9.9")
        try:
            cookie, csrf = self._csrf(port, "/password", f"denz_member={tok}")
            st, h, body = self._post(port, "/password",
                                     {"old_password": "lama123",
                                      "new_password": "baru456",
                                      "confirm_password": "baru456",
                                      "_csrf": csrf},
                                     cookie)
            assert st == 200 and "berhasil" in body.lower()
            assert webdenz.dec_secret(webdenz.load_member("ganti")["password"]) == "baru456"
        finally:
            srv.shutdown()
            t.join(timeout=3)

    def test_rate_limit_login(self, wd):
        import webdenz
        cfg = webdenz.load_config()
        cfg["rate_max_attempts"] = 3
        cfg["rate_window_sec"] = 60
        webdenz.save_config(cfg)
        srv, t = self._start(wd)
        port = srv.server_address[1]
        try:
            cookie, tok = self._csrf(port, "/login")
            msgs = []
            for _ in range(4):
                st, h, body = self._post(port, "/login",
                                         {"username": "nobody", "password": "x",
                                          "_csrf": tok}, cookie)
                msgs.append("terlalu banyak" in body)
            assert any(msgs), "harus ada respon rate-limit"
        finally:
            srv.shutdown()
            t.join(timeout=3)

    def test_owner_delete_member(self, wd):
        import webdenz
        srv, t = self._start(wd)
        port = srv.server_address[1]
        webdenz.create_member("korban", "pwkorban", "Korban")
        webdenz.issue_member_session("korban", "1.1.1.1")
        try:
            cookie, tok = self._csrf(port, "/owner")
            st, h, body = self._post(port, "/owner/login",
                                     {"username": "denzyx", "password": "ownerpw",
                                      "_csrf": tok}, cookie)
            assert st in (302, 303)
            ocookie = f"denz_owner={self._cookie_val(h.get('Set-Cookie', ''), 'denz_owner')}"
            cookie, tok = self._csrf(port, "/owner/member/korban", ocookie)
            st, h, body = self._post(port, "/owner/member/korban",
                                     {"action": "delete", "_csrf": tok},
                                     f"{ocookie}; {cookie}")
            assert st in (302, 303) and "/owner" in h.get("Location", "")
            assert webdenz.load_member("korban") is None
            assert not webdenz.session_md_path("korban").exists()
            rows = webdenz.read_log("admin")
            assert any('"action": "delete"' in r and "korban" in r for r in rows)
        finally:
            srv.shutdown()
            t.join(timeout=3)

    def test_chat_requires_login(self, wd):
        import webdenz
        srv, t = self._start(wd)
        port = srv.server_address[1]
        try:
            st, h, body = self._get(port, "/chat")
            assert st == 302 and "/login" in h.get("Location", "")
            # api tanpa cookie → 401
            st, h, body = self._post(port, "/api/chat", {"message": "x"})
            assert st == 401
        finally:
            srv.shutdown()
            t.join(timeout=3)

    def test_waf_blocks_attacker(self, wd):
        """Serangan (UA Burp / honeypot path) → 403 + ban IP permanen."""
        import webdenz
        import waf
        srv, t = self._start(wd)
        port = srv.server_address[1]
        ip = "203.0.113.7"
        cf = {"CF-Connecting-IP": ip}
        try:
            # 1) UA alat peretas → 403 & di-ban
            st, h, body = self._get(
                port, "/", extra_headers={**cf, "User-Agent": "Burp Suite Free"})
            assert st == 403
            assert waf.is_banned(ip)
            # 2) request berikutnya dari IP sama (path wajar) tetap 403
            st, h, body = self._get(port, "/login", extra_headers=cf)
            assert st == 403
            # 3) IP lain tetap bisa akses
            st, h, body = self._get(port, "/",
                                    extra_headers={"CF-Connecting-IP": "198.51.100.9"})
            assert st == 302
            # 4) unban → akses normal lagi
            assert waf.unban(ip)
            st, h, body = self._get(port, "/login", extra_headers=cf)
            assert st == 200 and "denzyx" in body.lower()
        finally:
            srv.shutdown()
            t.join(timeout=3)

    def test_waf_honeypot_403(self, wd):
        """Path honeypot ditolak & IP (non-loopback) di-ban."""
        import webdenz
        import waf
        srv, t = self._start(wd)
        port = srv.server_address[1]
        cf = {"CF-Connecting-IP": "198.51.100.42"}
        try:
            st, h, body = self._get(port, "/wp-login.php", extra_headers=cf)
            assert st == 403
            assert waf.is_banned("198.51.100.42")
            # path traversal juga ditolak
            st, h, body = self._get(port, "/../../etc/passwd",
                                    extra_headers={"CF-Connecting-IP": "198.51.100.43"})
            assert st == 403
        finally:
            srv.shutdown()
            t.join(timeout=3)

    def test_waf_loopback_not_banned(self, wd):
        """Koneksi lokal (tanpa CF header) tidak pernah di-ban permanen."""
        import webdenz
        import waf
        srv, t = self._start(wd)
        port = srv.server_address[1]
        try:
            st, h, body = self._get(port, "/wp-login.php")
            assert st == 403          # request itu ditolak...
            assert not waf.is_banned("127.0.0.1")  # ...tapi tak di-ban
            st, h, body = self._get(port, "/login")
            assert st == 200          # akses normal tetap jalan
        finally:
            srv.shutdown()
            t.join(timeout=3)

    def test_waf_secure_cookie_behind_tunnel(self, wd):
        """Request via CF-Connecting-IP (edge TLS) → cookie Secure + HSTS."""
        import webdenz
        srv, t = self._start(wd)
        port = srv.server_address[1]
        try:
            st, h, body = self._get(port, "/login",
                                    extra_headers={"CF-Connecting-IP": "1.2.3.4"})
            assert st == 200
            assert "Secure" in h.get("Set-Cookie", "")
            assert "Strict-Transport-Security" in h
            # langsung (tanpa tunnel) → tanpa Secure/HSTS
            st, h, body = self._get(port, "/login")
            assert "Secure" not in h.get("Set-Cookie", "")
            assert "Strict-Transport-Security" not in h
        finally:
            srv.shutdown()
            t.join(timeout=3)

    def test_waf_scan_404_bans(self, wd):
        """Banyak 404 ke path berbeda → ban (endpoint scan)."""
        import webdenz
        import waf
        srv, t = self._start(wd)
        port = srv.server_address[1]
        cf = {"CF-Connecting-IP": "203.0.113.99"}
        try:
            cfg = webdenz.load_config()
            cfg["ban_scan_threshold"] = 5
            webdenz.save_config(cfg)
            statuses = []
            for i in range(5):
                st, h, body = self._get(port, f"/x{i}/scan{i}", extra_headers=cf)
                statuses.append(st)
            assert statuses[-1] == 403      # ban berlaku di threshold
            assert waf.is_banned("203.0.113.99")
        finally:
            srv.shutdown()
            t.join(timeout=3)

    def test_waf_bruteforce_bans(self, wd):
        """Gagal login berulang → IP di-ban setelah threshold."""
        import webdenz
        import waf
        srv, t = self._start(wd)
        port = srv.server_address[1]
        cf = {"CF-Connecting-IP": "198.51.100.77"}
        try:
            cfg = webdenz.load_config()
            cfg["rate_max_attempts"] = 3
            cfg["ban_fail_threshold"] = 5
            webdenz.save_config(cfg)
            cookie, tok = self._csrf(port, "/login")
            for _ in range(4):
                st, h, body = self._post(port, "/login",
                                         {"username": "nobody",
                                          "password": "x", "_csrf": tok},
                                         cookie, extra_headers=cf)
            assert not waf.is_banned("198.51.100.77")
            st, h, body = self._post(port, "/login",
                                     {"username": "nobody", "password": "x",
                                      "_csrf": tok}, cookie, extra_headers=cf)
            assert waf.is_banned("198.51.100.77")
            # sekarang semua request dari IP tsb → 403
            st, h, body = self._get(port, "/login", extra_headers=cf)
            assert st == 403
        finally:
            srv.shutdown()
            t.join(timeout=3)

    def test_waf_owner_security_page(self, wd):
        """Halaman /owner/security menampilkan ban & bisa unban."""
        import webdenz
        import waf
        srv, t = self._start(wd)
        port = srv.server_address[1]
        waf.ban("203.0.113.5", "honeypot path 'wp-login.php'", path="/wp-login.php")
        try:
            cookie, tok = self._csrf(port, "/owner")
            st, h, body = self._post(port, "/owner/login",
                                     {"username": "denzyx", "password": "ownerpw",
                                      "_csrf": tok}, cookie)
            assert st in (302, 303)
            ocookie = f"denz_owner={self._cookie_val(h.get('Set-Cookie', ''), 'denz_owner')}"
            st, h, body = self._get(port, "/owner/security", ocookie)
            assert st == 200 and "203.0.113.5" in body and "Unban" in body
            # unban via form
            cookie, tok = self._csrf(port, "/owner/security", ocookie)
            st, h, body = self._post(port, "/owner/security",
                                     {"action": "unban", "ip": "203.0.113.5",
                                      "_csrf": tok}, f"{ocookie}; {cookie}")
            assert st in (200, 302, 303)
            assert not waf.is_banned("203.0.113.5")
        finally:
            srv.shutdown()
            t.join(timeout=3)

    def test_reg_notify_detail(self, wd, monkeypatch):
        """Notifikasi registrasi berisi detail IP/software/waktu (bukan cuma IP)."""
        import webdenz
        h = object.__new__(webdenz.Handler)
        h.client_address = ("2001:448a:3071:1386:22ea:9ca1:8f9d:e5fd", 4321)
        h._real_ip = "2001:448a:3071:1386:22ea:9ca1:8f9d:e5fd"
        h.headers = {
            "User-Agent": "Mozilla/5.0 (Linux; Android 14; SM-A156E) "
                          "AppleWebKit/537.36 (KHTML, like Gecko) "
                          "Chrome/126.0.0.0 Mobile Safari/537.36",
            "CF-Connecting-IP": "2001:448a:3071:1386:22ea:9ca1:8f9d:e5fd",
            "X-Forwarded-For": "10.0.0.5, 2001:448a:3071:1386:22ea:9ca1:8f9d:e5fd",
            "Referer": "https://t.me/c/12345/100",
        }
        import denzbot
        captured = []
        monkeypatch.setattr(denzbot, "tg_notify", lambda text: captured.append(text))
        h._reg_notify("pett", "test")
        for _ in range(100):
            if captured:
                break
            import time
            time.sleep(0.1)
        assert captured, "notifikasi tidak terkirim"
        msg = captured[0]
        assert "REGISTRASI BARU" in msg
        assert "pett" in msg and "test" in msg
        assert "2001:448a" in msg  # IP
        assert "Chrome" in msg and "Android" in msg  # software/hardware
        assert "mobile" in msg  # device
        assert "Waktu" in msg and "2026" in msg  # tanggal/jam/tahun
        assert "Referer" in msg
        assert "Cek owner panel" in msg

    def test_keepalive_no_false_429(self, wd):
        """HTTP/1.1 keep-alive: banyak request pada satu koneksi TIDAK boleh
        kena 429 'terlalu banyak koneksi' (release slot per-request)."""
        import http.client
        import webdenz
        srv, t = self._start(wd)
        port = srv.server_address[1]
        try:
            conn = http.client.HTTPConnection("127.0.0.1", port, timeout=10)
            codes = []
            for _ in range(12):  # > max_per_ip (8) pada koneksi yang sama
                conn.request("GET", "/login")
                r = conn.getresponse()
                codes.append(r.status)
                r.read()
            conn.close()
            assert 429 not in codes, f"false 429: {codes}"
        finally:
            srv.shutdown()
            srv.server_close()

    def test_404_and_405_return_marquee_over_http(self, wd):
        """404 & 405 lewat HTTP: status asli + halaman marquee KAMU BODOH."""
        import urllib.request
        import urllib.error
        import webdenz
        srv, t = self._start(wd)
        port = srv.server_address[1]
        try:
            st, h, body = self._get(port, "/gak-ada-page-ini")
            assert st == 404
            assert "KAMU BODOH BANGET SIH" in body

            req = urllib.request.Request(
                f"http://127.0.0.1:{port}/login", method="PUT")
            try:
                with urllib.request.urlopen(req, timeout=10) as r:
                    st = r.status
                    body = r.read().decode()
            except urllib.error.HTTPError as e:
                st, body = e.code, e.read().decode()
            assert st == 405
            assert "KAMU BODOH BANGET SIH" in body
        finally:
            srv.shutdown()
            srv.server_close()


class TestVisitors:
    """Test panel pengunjung (helpers sendiri — jangan subclass TestHTTP,
    supaya test TestHTTP tidak jalan dua kali)."""

    def _start(self, wd):
        import webdenz
        srv = webdenz.ThreadingHTTPServer(("127.0.0.1", 0), webdenz.Handler)
        t = threading.Thread(target=srv.serve_forever, daemon=True)
        t.start()
        return srv, t

    def _post(self, port, path, data=None, cookie="", extra_headers=None):
        import urllib.request
        body = urllib.parse.urlencode(data or {}).encode()
        hdrs = {"Cookie": cookie}
        hdrs.update(extra_headers or {})
        req = urllib.request.Request(
            f"http://127.0.0.1:{port}{path}", data=body, method="POST",
            headers=hdrs)
        opener = _no_redirect_opener()
        try:
            with opener.open(req, timeout=10) as r:
                return r.status, r.headers, r.read().decode()
        except urllib.error.HTTPError as e:
            return e.code, e.headers, e.read().decode()

    def _get(self, port, path, cookie="", extra_headers=None):
        import urllib.request
        hdrs = {"Cookie": cookie}
        hdrs.update(extra_headers or {})
        req = urllib.request.Request(
            f"http://127.0.0.1:{port}{path}", headers=hdrs)
        opener = _no_redirect_opener()
        try:
            with opener.open(req, timeout=10) as r:
                return r.status, r.headers, r.read().decode()
        except urllib.error.HTTPError as e:
            return e.code, e.headers, e.read().decode()

    def _cookie_val(self, setcookie, name):
        for part in str(setcookie).split(","):
            part = part.strip()
            if part.startswith(name + "="):
                return part.split("=")[1].split(";")[0]
        return ""

    def _csrf_from(self, body):
        import re
        mt = re.search(r'name="_csrf" value="([^"]+)"', body)
        return mt.group(1) if mt else None

    def _csrf(self, port, path, cookie=""):
        st, h, body = self._get(port, path, cookie)
        assert st == 200
        raw = self._cookie_val(h.get("Set-Cookie", ""), "denz_csrf")
        tok = self._csrf_from(body)
        assert raw and tok
        joined = (cookie + "; " if cookie else "") + f"denz_csrf={raw}"
        return joined, tok

    def test_visitor_recorded_and_panel(self, wd):
        """Setiap request terekam; owner panel /owner/visitors menampilkannya."""
        import webdenz
        import waf
        srv, t = self._start(wd)
        port = srv.server_address[1]
        try:
            self._get(port, "/")
            cookie, tok = self._csrf(port, "/owner")
            st, h, body = self._post(port, "/owner/login",
                                     {"username": "denzyx", "password": "ownerpw",
                                      "_csrf": tok}, cookie)
            assert st in (302, 303)
            ocookie = f"denz_owner={self._cookie_val(h.get('Set-Cookie', ''), 'denz_owner')}"
            st, h, body = self._get(port, "/owner/visitors", ocookie)
            assert st == 200 and "Pengunjung" in body
            assert "127.0.0.1" in body
            st, h, body = self._get(port, "/owner/visitor/127.0.0.1", ocookie)
            assert st == 200 and "Detail Visitor" in body
            # ban dari panel pengunjung (CSRF)
            cookie, tok = self._csrf(port, "/owner/visitors", ocookie)
            st, h, body = self._post(port, "/owner/visitors",
                                     {"action": "ban", "ip": "203.0.113.5",
                                      "_csrf": tok}, f"{ocookie}; {cookie}")
            assert waf.is_banned("203.0.113.5")
        finally:
            srv.shutdown()
            t.join(timeout=3)

    def test_login_notify_detail(self, wd, monkeypatch):
        """Notifikasi login member juga detail (bukan cuma IP)."""
        import webdenz
        h = object.__new__(webdenz.Handler)
        h.client_address = ("127.0.0.1", 4321)
        h._real_ip = "203.0.113.9"
        h.headers = {
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                          "AppleWebKit/537.36 (KHTML, like Gecko) "
                          "Chrome/124.0.0.0 Safari/537.36",
            "CF-Connecting-IP": "203.0.113.9",
            "X-Forwarded-For": "203.0.113.9",
            "Referer": "http://localhost:8000/login",
        }
        import denzbot
        captured = []
        monkeypatch.setattr(denzbot, "tg_notify", lambda text: captured.append(text))
        h._login_notify("budi")
        for _ in range(100):
            if captured:
                break
            import time
            time.sleep(0.1)
        assert captured, "notifikasi login tidak terkirim"
        msg = captured[0]
        assert "LOGIN MEMBER" in msg
        assert "budi" in msg
        assert "203.0.113.9" in msg
        assert "Windows" in msg and "Chrome" in msg
        assert "Waktu" in msg

    def test_remind_expiring(self, wd, monkeypatch):
        """Member yang hampir habis (H-2/H-1) diingatkan owner + member."""
        import webdenz
        from datetime import datetime, timedelta
        import denzbot

        # aktif, habis H-2 → harus muncul
        m = webdenz.create_member("h2", "pw1234", "H2")
        m["status"] = "active"
        m["expires_at"] = (datetime.now() + timedelta(days=2)).isoformat()
        m["tg_chat_id"] = "10001"
        webdenz.save_member(m)
        # aktif, habis H-5 → tidak usah
        m2 = webdenz.create_member("h5", "pw1234", "H5")
        m2["status"] = "active"
        m2["expires_at"] = (datetime.now() + timedelta(days=5)).isoformat()
        webdenz.save_member(m2)
        # sudah lewat → tidak usah
        m3 = webdenz.create_member("gone", "pw1234", "Gone")
        m3["status"] = "active"
        m3["expires_at"] = (datetime.now() - timedelta(days=1)).isoformat()
        webdenz.save_member(m3)

        import denzbot as db
        sent = []
        monkeypatch.setattr(db, "tg_notify", lambda text: sent.append(text))
        monkeypatch.setattr(db, "tg_send", lambda cid, text: sent.append((cid, text)))

        db._remind_expiring("OWNER", {})
        joined = "\n".join(str(s) for s in sent)
        assert "h2" in joined
        assert "h5" not in joined
        assert "gone" not in joined
        assert "10001" in joined  # member langsung dapat notif

        # kedua kalinya → tidak spam (state tersimpan)
        sent.clear()
        db._remind_expiring("OWNER", {})
        assert not sent

