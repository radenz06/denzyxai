"""Smoke test tool dscli tanpa menyentuh jaringan/perangkat.

Jalan dengan:  python3 -m pytest -q tests/
"""

import os
import sys
import tempfile

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import dscli  # noqa: E402


def _tmp():
    return tempfile.mkdtemp(prefix="denz_test_")


# ---------------------------------------------------------------------------
# Tool dasar
# ---------------------------------------------------------------------------

def test_tools_terdaftar():
    names = {t["function"]["name"] for t in dscli.TOOLS}
    # 'task' di-handle khusus di app utama, bukan TOOL_IMPL
    assert dscli.TOOL_IMPL.keys() <= names
    assert {"read", "write", "edit", "glob", "grep", "bash"} <= names
    assert "sdk" in dscli.TOOL_IMPL


def test_bash_exit_code():
    r = dscli.tool_bash("true")
    assert "exit=0" in r


def test_bash_error_surface():
    r = dscli.tool_bash("exit 3")
    assert "exit=3" in r


def test_write_read_roundtrip():
    d = _tmp()
    p = os.path.join(d, "a", "b", "x.txt")
    assert "OK" in dscli.tool_write(p, "halo")
    out = dscli.tool_read(p)
    assert "halo" in out


def test_read_missing():
    assert "tidak ada" in dscli.tool_read("/no/such/file.txt")


def test_edit_replace():
    d = _tmp()
    p = os.path.join(d, "f.py")
    dscli.tool_write(p, "a\nb\nc\n")
    dscli.tool_edit(p, "b", "B")
    assert "B" in dscli.tool_read(p)
    assert "b" not in dscli.tool_read(p).replace("b\nc", "B\nc")  # ganti 1x


def test_edit_missing_old():
    d = _tmp()
    p = os.path.join(d, "f.txt")
    dscli.tool_write(p, "abc")
    assert "tidak ditemukan" in dscli.tool_edit(p, "zzz", "x")


def test_glob():
    d = _tmp()
    dscli.tool_write(os.path.join(d, "one.py"), "")
    dscli.tool_write(os.path.join(d, "two.txt"), "")
    hits = dscli.tool_glob("*.py", path=d)
    assert "one.py" in hits and "two.txt" not in hits


def test_grep():
    d = _tmp()
    dscli.tool_write(os.path.join(d, "m.py"), "def jalan():\n    pass\n")
    r = dscli.tool_grep("def jalan", path=d)
    assert "m.py" in r


# ---------------------------------------------------------------------------
# Dev tools
# ---------------------------------------------------------------------------

def test_scaffold_python():
    d = _tmp()
    r = dscli.tool_scaffold("python", path=os.path.join(d, "app"))
    assert "main.py" in r
    assert os.path.exists(os.path.join(d, "app", "main.py"))


def test_build_detect_dan_run():
    d = _tmp()
    dscli.tool_scaffold("python", path=os.path.join(d, "app"))
    info = dscli._detect_project(os.path.join(d, "app"))
    assert info["lang"] == "Python"
    assert info["main"] == "main.py"
    r = dscli.tool_build(os.path.join(d, "app"), "run")
    assert "Hello, Denz!" in r


def test_build_android_butuh_sdk(monkeypatch):
    monkeypatch.setattr(dscli, "_android_sdk_root", lambda: None)
    d = _tmp()
    for f in ("settings.gradle", "build.gradle", "gradlew"):
        open(os.path.join(d, f), "w").close()
    r = dscli.tool_build(d, "build")
    assert "sdk(action='setup')" in r


def test_build_android_sdk_ada(monkeypatch):
    monkeypatch.setattr(dscli, "_android_sdk_root", lambda: "/tmp/sdk")
    d = _tmp()
    for f in ("settings.gradle", "build.gradle", "gradlew"):
        open(os.path.join(d, f), "w").close()
    r = dscli.tool_build(d, "build")
    assert "ANDROID_HOME=/tmp/sdk" in r


def test_tree():
    d = _tmp()
    dscli.tool_write(os.path.join(d, "a.py"), "")
    os.makedirs(os.path.join(d, "sub"))
    r = dscli.tool_tree(d, depth=2)
    assert "a.py" in r and "sub" in r


def test_debug_analyze_log():
    d = _tmp()
    p = os.path.join(d, "crash.log")
    with open(p, "w") as fh:
        fh.write("mulai\nTraceback (most recent call last):\n"
                 '  File "x.py", line 3, in <module>\n    a = b\n'
                 "NameError: name 'b' is not defined\n")
    r = dscli.tool_debug(target=p, mode="analyze")
    assert "NameError" in r


def test_git_ops():
    d = _tmp()
    dscli.tool_write(os.path.join(d, "readme.md"), "test")
    r = dscli.tool_git("init", workdir=d)
    assert "exit=0" in r
    dscli.tool_git("add", workdir=d)
    dscli.tool_git("commit", args=["awal"], workdir=d)
    log = dscli.tool_git("log", workdir=d)
    assert "awal" in log


def test_pkg_detect_tanpa_manager():
    d = _tmp()
    r = dscli.tool_pkg("check", workdir=d)
    assert "tidak ada package manager" in r


def test_logs_auto_cari():
    d = _tmp()
    p = os.path.join(d, "app.log")
    with open(p, "w") as fh:
        fh.write("\n".join(f"baris{i}" for i in range(60)))
    r = dscli.tool_logs(path=p, lines=5)
    assert "baris59" in r


def test_sdk_check_ramah():
    r = dscli.tool_sdk("check")
    assert "ANDROID_HOME" in r
