# -*- coding: utf-8 -*-
"""Test waf — Web Application Firewall (real IP, deteksi, ban persist)."""

import importlib
import json
import os

import pytest


@pytest.fixture(scope="function", autouse=True)
def wd(tmp_path):
    """Isolasi waf/webdenz ke tmp dir per-test."""
    os.environ["WEBDENZ_CONFIG"] = str(tmp_path / "webconfig.json")
    os.environ["WEBDENZ_DATA"] = str(tmp_path / "webdata")
    import waf
    importlib.reload(waf)
    return waf


class TestRealIP:
    def test_loopback_trusts_cf_header(self, wd):
        hdr = {"CF-Connecting-IP": "203.0.113.7"}
        assert wd.get_real_ip(("127.0.0.1", 54321), hdr) == "203.0.113.7"

    def test_loopback_trusts_xff_first(self, wd):
        hdr = {"X-Forwarded-For": "198.51.100.3, 203.0.113.1"}
        assert wd.get_real_ip(("127.0.0.1", 1), hdr) == "198.51.100.3"

    def test_nonloopback_ignores_header(self, wd):
        # akses langsung dari IP lain — header TIDAK boleh dipercaya
        hdr = {"CF-Connecting-IP": "203.0.113.7"}
        assert wd.get_real_ip(("203.0.113.7", 54321), hdr) == "203.0.113.7"
        assert wd.get_real_ip(("1.2.3.4", 54321), hdr) == "1.2.3.4"

    def test_unknown_value_ignored(self, wd):
        hdr = {"CF-Connecting-IP": "unknown"}
        assert wd.get_real_ip(("127.0.0.1", 1), hdr) == "127.0.0.1"


class TestScanSignal:
    def test_attack_ua(self, wd):
        assert wd.scan_signal("1.1.1.1", "Mozilla Burp Suite Free", "/", "")
        assert wd.scan_signal("1.1.1.1", "sqlmap/1.7", "/", "")
        assert not wd.scan_signal("1.1.1.1", "Mozilla/5.0 (Windows NT 10.0)",
                                  "/login", "")

    def test_honeypot_path(self, wd):
        assert "wp-login.php" in wd.scan_signal("1.1.1.1", "", "/wp-login.php")
        assert wd.scan_signal("1.1.1.1", "", "/.git/config")
        assert not wd.scan_signal("1.1.1.1", "", "/login")

    def test_path_traversal(self, wd):
        assert wd.scan_signal("1.1.1.1", "", "/etc/passwd")
        assert wd.scan_signal("1.1.1.1", "", "/../../../etc/shadow")
        assert wd.scan_signal("1.1.1.1", "", "/foo%2e%2e/bar")

    def test_injection(self, wd):
        assert wd.scan_signal("1.1.1.1", "", "/search",
                              "q=1' or '1'='1")
        assert wd.scan_signal("1.1.1.1", "", "/api/chat",
                              "x=<script>alert(1)</script>")
        assert not wd.scan_signal("1.1.1.1", "", "/api/chat",
                                  "halo apa kabar")

    def test_legit_paths_clean(self, wd):
        for p in ("/", "/login", "/register", "/chat", "/status",
                  "/password", "/owner", "/owner/member/citra",
                  "/api/status", "/api/me", "/api/chat",
                  "/api/chat/stream", "/qr", "/logout", "/admin/add"):
            assert wd.scan_signal("1.1.1.1", "Python-urllib/3.11",
                                  p, "") is None, p


class TestBanStore:
    def test_ban_persist_and_unban(self, wd):
        wd.ban("203.0.113.7", "honeypot path 'wp-login.php'", ua="x",
               path="/wp-login.php")
        assert wd.is_banned("203.0.113.7")
        # persist ke disk
        data = json.loads(wd._bans_file().read_text(encoding="utf-8"))
        assert "203.0.113.7" in data
        assert data["203.0.113.7"]["reason"].startswith("honeypot")
        # unban
        assert wd.unban("203.0.113.7")
        assert not wd.is_banned("203.0.113.7")
        assert not wd.unban("203.0.113.7")

    def test_loopback_never_banned(self, wd):
        wd.ban("127.0.0.1", "x")
        wd.ban("::1", "x")
        assert not wd.is_banned("127.0.0.1")
        assert not wd.is_banned("::1")

    def test_ban_increments_count(self, wd):
        wd.ban("198.51.100.9", "alasan A", path="/a")
        wd.ban("198.51.100.9", "alasan B", path="/b")
        e = wd.list_bans()["198.51.100.9"]
        assert e["count"] == 2
        assert e["reason"] == "alasan B"

    def test_flag_bans_after_threshold(self, wd):
        for _ in range(5):
            assert not wd.flag("1.2.3.4", "login", "brute-force")
        assert not wd.is_banned("1.2.3.4")
        wd.flag("1.2.3.4", "login", "brute-force")  # ke-6 → ban
        assert wd.is_banned("1.2.3.4")
        assert not wd.flag("127.0.0.1", "login", "brute-force")


class TestRecord404:
    def test_scan_bans_after_threshold(self, wd):
        assert not wd.record_404("5.6.7.8", "/path1")
        assert not wd.record_404("5.6.7.8", "/path2")
        assert wd.record_404("5.6.7.8", "/path3",
                             threshold=3)  # ban di threshold ke-3
        assert wd.is_banned("5.6.7.8")

    def test_same_path_not_scan(self, wd):
        for _ in range(10):
            assert not wd.record_404("9.9.9.9", "/satu", threshold=5)
        assert not wd.is_banned("9.9.9.9")
