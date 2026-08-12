# -*- coding: utf-8 -*-
"""Test track — perekam pengunjung web (IP, lokasi, software)."""

import importlib
import json
import os

import pytest


@pytest.fixture(scope="function", autouse=True)
def tr(tmp_path):
    """Isolasi track ke tmp dir (env var + reload modul, tanpa network)."""
    os.environ["WEBDENZ_DATA"] = str(tmp_path / "webdata")
    os.environ["WEBDENZ_TRACK_GEO"] = "0"
    import track
    importlib.reload(track)
    track.set_geo(False)
    return track


class TestUAParser:
    def test_desktop_chrome(self, tr):
        info = tr.parse_ua(
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
            "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126.0 Safari/537.36")
        assert info["browser"] == "Chrome"
        assert info["os"] == "Windows 10/11"
        assert info["device"] == "desktop"
        assert not info["is_bot"]

    def test_mobile_android(self, tr):
        info = tr.parse_ua(
            "Mozilla/5.0 (Linux; Android 14; SM-A556E) AppleWebKit/537.36 "
            "(KHTML, like Gecko) Chrome/125.0.0.0 Mobile Safari/537.36")
        assert info["browser"] == "Chrome"
        assert info["os"] == "Android"
        assert info["device"] == "mobile"

    def test_iphone_safari(self, tr):
        info = tr.parse_ua(
            "Mozilla/5.0 (iPhone; CPU iPhone OS 17_5 like Mac OS X) "
            "AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.5 Mobile/15E148 "
            "Safari/604.1")
        assert info["browser"] == "Safari"
        assert info["os"] == "iOS"
        assert info["device"] == "mobile"

    def test_firefox_linux(self, tr):
        info = tr.parse_ua("Mozilla/5.0 (X11; Linux x86_64; rv:127.0) "
                           "Gecko/20100101 Firefox/127.0")
        assert info["browser"] == "Firefox"
        assert info["os"] == "Linux"
        assert info["device"] == "desktop"

    def test_bot(self, tr):
        info = tr.parse_ua("Mozilla/5.0 (compatible; Googlebot/2.1; "
                           "+http://www.google.com/bot.html)")
        assert info["is_bot"]
        assert info["device"] == "bot"
        assert "googlebot" in info["browser"].lower()

    def test_scanner_tool(self, tr):
        info = tr.parse_ua("sqlmap/1.7.2#stable")
        assert info["is_bot"]
        assert info["device"] == "bot"

    def test_curl(self, tr):
        info = tr.parse_ua("curl/8.5.0")
        assert info["is_bot"]
        assert info["browser"] == "curl"


class TestIpClass:
    def test_public(self, tr):
        assert tr.ip_class("8.8.8.8") == "public"
        assert tr.ip_class("45.55.4.1") == "public"

    def test_private(self, tr):
        assert tr.ip_class("192.168.1.10") == "private"
        assert tr.ip_class("10.0.0.1") == "private"
        assert tr.ip_class("172.16.0.1") == "private"

    def test_loopback(self, tr):
        assert tr.ip_class("127.0.0.1") == "loopback"
        assert tr.ip_class("::1") == "loopback"

    def test_invalid(self, tr):
        assert tr.ip_class("") == "invalid"
        assert tr.ip_class("not-an-ip") == "invalid"


class TestVisit:
    def test_aggregates(self, tr):
        ua = ("Mozilla/5.0 (Linux; Android 14) AppleWebKit/537.36 Chrome/125.0 "
              "Mobile Safari/537.36")
        hdrs = {"User-Agent": ua, "CF-Connecting-IP": "45.55.4.1",
                "X-Forwarded-For": "45.55.4.1, 10.1.1.1"}
        tr.visit("45.55.4.1", hdrs, "/login", method="POST",
                 peer=("127.0.0.1", 51234))
        tr.visit("45.55.4.1", hdrs, "/api/login", method="POST",
                 peer=("127.0.0.1", 51234))
        tr.visit("45.55.4.1", hdrs, "/", method="GET",
                 peer=("127.0.0.1", 51234))
        tr.status("45.55.4.1", 200)
        tr.flush()

        v = tr.get("45.55.4.1")
        assert v["visits"] == 3
        assert v["ip_class"] == "public"
        assert v["cf_ip"] == "45.55.4.1"
        assert v["peer"] == "127.0.0.1"
        assert v["browser"] == "Chrome"
        assert v["os"] == "Android"
        assert v["device"] == "mobile"
        assert v["first_seen"] == v["last_seen"]
        assert v["methods"].get("POST") == 2
        assert v["methods"].get("GET") == 1
        assert v["statuses"].get("200") == 1
        assert set(v["paths"]) == {"/login", "/api/login", "/"}

    def test_private_ip_capture(self, tr):
        tr.visit("192.168.1.20", {"User-Agent": "curl/8.5"}, "/",
                 peer=("192.168.1.20", 4000))
        tr.flush()
        v = tr.get("192.168.1.20")
        assert v["ip_class"] == "private"
        assert v["is_bot"] is True

    def test_file_and_log_written(self, tr):
        tr.visit("203.0.113.9", {"User-Agent": "Mozilla/5.0 Chrome/126"}, "/")
        tr.flush()
        raw = tr.visitors_file().read_text(encoding="utf-8")
        assert "203.0.113.9" in raw
        log = (tr.visitors_file().parent / "logs" / "visitors.log")
        assert "203.0.113.9" in log.read_text(encoding="utf-8")

    def test_recent_log(self, tr):
        tr.visit("203.0.113.10", {"User-Agent": "curl/8.5"}, "/wp-login.php",
                 method="POST")
        rows = tr.recent("203.0.113.10")
        assert rows and rows[0]["path"] == "/wp-login.php"
        assert rows[0]["method"] == "POST"

    def test_log_throttled_per_ip(self, tr):
        tr.visit("203.0.113.11", {"User-Agent": "curl"}, "/a")
        tr.visit("203.0.113.11", {"User-Agent": "curl"}, "/b")
        rows = tr.recent("203.0.113.11")
        assert len(rows) == 1  # maks 1 baris/menit/IP
        tr.clear()
        assert tr.recent("203.0.113.11") == []

    def test_summary(self, tr):
        tr.visit("203.0.113.12", {"User-Agent": "curl"}, "/")
        tr.visit("203.0.113.13", {"User-Agent": "Mozilla/5.0 Mobile"}, "/")
        s = tr.summary()
        assert s["total"] == 2
        assert s["visits"] == 2
        assert s["bots"] == 1
        assert s["mobile"] == 1
        assert s["today"] >= 1

    def test_clear(self, tr):
        tr.visit("203.0.113.14", {"User-Agent": "curl"}, "/")
        tr.flush()
        assert tr.get("203.0.113.14")
        tr.clear()
        assert not tr.get("203.0.113.14")
        assert not tr.visitors_file().exists()
