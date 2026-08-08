# -*- coding: utf-8 -*-
"""Test webdenz — member/admin system (isolasi: tmp dir + env var)."""

import importlib
import json
import os
import threading
import urllib.parse
import urllib.request

import pytest


@pytest.fixture(scope="function", autouse=True)
def wd(tmp_path):
    """Isolasi webdenz ke tmp dir per-test (env var + reload modul)."""
    os.environ["WEBDENZ_CONFIG"] = str(tmp_path / "webconfig.json")
    os.environ["WEBDENZ_DATA"] = str(tmp_path / "webdata")
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
        assert "Minta QR ke Owner" in html
        assert "https://t.me/colipopi?text=" in html
        assert "bisa kirimkan qr sekarang" in urllib.parse.unquote(
            webdenz._pay_tg_link(cfg, webdenz.load_member("budi")))
        # aktif → tombol hilang
        m = webdenz.load_member("budi")
        m["status"] = "active"
        from datetime import datetime, timedelta
        m["expires_at"] = (datetime.now() + timedelta(days=30)).isoformat()
        webdenz.save_member(m)
        html2 = webdenz._status_page(webdenz.load_member("budi"))
        assert "Minta QR ke Owner" not in html2


class TestHTTP:
    def _start(self, wd):
        import webdenz
        srv = webdenz.ThreadingHTTPServer(("127.0.0.1", 0), webdenz.Handler)
        t = threading.Thread(target=srv.serve_forever, daemon=True)
        t.start()
        return srv, t

    def _post(self, port, path, data=None, cookie=""):
        import urllib.request
        body = urllib.parse.urlencode(data or {}).encode()
        req = urllib.request.Request(
            f"http://127.0.0.1:{port}{path}", data=body, method="POST",
            headers={"Cookie": cookie})
        opener = _no_redirect_opener()
        try:
            with opener.open(req, timeout=10) as r:
                return r.status, r.headers, r.read().decode()
        except urllib.error.HTTPError as e:
            return e.code, e.headers, e.read().decode()

    def _get(self, port, path, cookie=""):
        import urllib.request
        req = urllib.request.Request(
            f"http://127.0.0.1:{port}{path}", headers={"Cookie": cookie})
        opener = _no_redirect_opener()
        try:
            with opener.open(req, timeout=10) as r:
                return r.status, r.headers, r.read().decode()
        except urllib.error.HTTPError as e:
            return e.code, e.headers, e.read().decode()

    def test_register_login_chat(self, wd, monkeypatch):
        import webdenz
        _mock_stream_chat(wd, monkeypatch)
        srv, t = self._start(wd)
        port = srv.server_address[1]
        try:
            # register
            st, h, body = self._post(port, "/register",
                                     {"username": "newbie", "display_name": "Newbie",
                                      "password": "pw123"})
            assert st == 200 and "pending" in body
            # login sebelum aktif → ditolak
            st, h, body = self._post(port, "/login",
                                     {"username": "newbie", "password": "pw123"})
            assert "pending" in body
            # activate
            m = webdenz.load_member("newbie")
            from datetime import datetime, timedelta
            m["status"] = "active"
            m["expires_at"] = (datetime.now() + timedelta(days=30)).isoformat()
            webdenz.save_member(m)
            # login sukses → redirect + cookie
            st, h, body = self._post(port, "/login",
                                     {"username": "newbie", "password": "pw123"})
            assert st in (302, 303)
            cookie = h.get("Set-Cookie", "")
            assert "denz_member=" in cookie
            tok = cookie.split("denz_member=")[1].split(";")[0]
            # chat page
            st, h, body = self._get(port, "/chat", f"denz_member={tok}")
            assert st == 200
            # api chat
            st, h, body = self._post(port, "/api/chat",
                                     {"message": "halo"}, f"denz_member={tok}")
            assert st == 200
            j = json.loads(body)
            assert j.get("reply") == "balasan AI"
            assert webdenz.load_member("newbie")["login_count"] >= 1
            # status page member
            st, h, body = self._get(port, "/status", f"denz_member={tok}")
            assert st == 200 and "newbie" in body
        finally:
            srv.shutdown()
            t.join(timeout=3)

    def test_owner_login_and_member_page(self, wd):
        import webdenz
        srv, t = self._start(wd)
        port = srv.server_address[1]
        webdenz.create_member("citra", "pwcitra", "Citra")
        try:
            # owner login
            st, h, body = self._post(port, "/owner/login",
                                     {"username": "denzyx", "password": "ownerpw"})
            assert st in (302, 303)
            otok = h.get("Set-Cookie", "").split("denz_owner=")[1].split(";")[0]
            # owner page
            st, h, body = self._get(port, "/owner", f"denz_owner={otok}")
            assert st == 200 and "citra" in body
            # member detail page — password terlihat oleh owner
            st, h, body = self._get(port, "/owner/member/citra",
                                    f"denz_owner={otok}")
            assert st == 200 and "pwcitra" in body
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
