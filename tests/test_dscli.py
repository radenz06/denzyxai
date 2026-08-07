"""Smoke test tool dscli tanpa menyentuh jaringan/perangkat.

Jalan dengan:  python3 -m pytest -q tests/
"""

import os
import sys
import tempfile
import time

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


# ---------------------------------------------------------------------------
# Akses & otomasi (v2.2)
# ---------------------------------------------------------------------------

def test_ssh_wajib_host():
    assert "host wajib" in dscli.tool_ssh(host=None)


def test_ssh_perlu_openssh(monkeypatch):
    monkeypatch.setattr(dscli, "_which", lambda _: None)
    r = dscli.tool_ssh(host="user@10.0.0.1")
    assert "openssh" in r


def test_download_wajib_url():
    assert "url wajib" in dscli.tool_download(None)


def test_bg_start_tail_kill():
    r = dscli.tool_bg("start", name="demo", command="echo halo bg")
    assert "pid" in r
    time.sleep(0.3)
    listing = dscli.tool_bg("list")
    assert "demo" in listing
    tail = dscli.tool_bg("tail", name="demo")
    assert "halo bg" in tail
    killed = dscli.tool_bg("kill", name="demo")
    assert "dihentikan" in killed
    assert "demo" not in dscli.tool_bg("list")


def test_bg_start_butuh_command():
    assert "butuh 'name' dan 'command'" in dscli.tool_bg("start", name="x")


def test_serve_start_status_stop():
    try:
        r = dscli.tool_serve("start", path=".", port=8765)
        assert "http://0.0.0.0:8765" in r
        assert "akses dari perangkat lain" in r
        assert "hidup" in dscli.tool_serve("status", port=8765)
    finally:
        dscli.tool_serve("stop", port=8765)
    assert "tidak jalan" in dscli.tool_serve("status", port=8765)


def test_root_check():
    r = dscli.tool_root()
    assert "uid=" in r or "su:" in r


def test_sys_sections():
    assert "OS" in dscli.tool_sys("os")
    assert "tidak dikenal" in dscli.tool_sys("banana")


def test_media_info_butuh_file():
    assert "action=info butuh 'file'" in dscli.tool_media()
    assert "action=play butuh 'file'" in dscli.tool_media("play")


# ---------------------------------------------------------------------------
# Voice chat (v2.3) — helper murni, tanpa mic
# ---------------------------------------------------------------------------

def _load_voice():
    import importlib.util
    path = os.path.join(os.path.dirname(__file__), "..", "voice-denz.py")
    spec = importlib.util.spec_from_file_location("voice_denz_mod", path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def test_voice_plain_markdown():
    v = _load_voice()
    assert v._plain("# Judul\n**tegas** `kode`") == "Judul\ntegas kode"
    assert "kode" not in v._plain("```\nkode blok\n```")


def test_voice_is_exit():
    v = _load_voice()
    assert v._is_exit("stop")
    assert v._is_exit("Matikan panggilan.")
    assert v._is_exit("sampai jumpa")
    assert not v._is_exit("beresin tugas sampai selesai")
    assert not v._is_exit("gimana cara stop kontak dicolok")


def test_voice_edge_rate():
    v = _load_voice()
    assert v._edge_rate(1.0) == "+0%"
    assert v._edge_rate(1.1) == "+10%"
    assert v._edge_rate(0.9) == "-10%"


def test_voice_default_suaranya_cewe():
    v = _load_voice()
    assert v.DEFAULT_VOICE == "id-ID-GadisNeural"


def test_voice_cli_help():
    import subprocess
    root = os.path.dirname(os.path.dirname(__file__))
    r = subprocess.run(
        ["python3", os.path.join(root, "denzyx.py"), "--help"],
        capture_output=True, text=True, timeout=30)
    assert "voice" in r.stdout.lower()


def test_voice_tts_text_buang_emoji():
    v = _load_voice()
    assert v._tts_text("Halo 😀 apa kabar ✨?") == "Halo apa kabar ?"
    assert v._tts_text("**Bold** ya") == "Bold ya"


def test_voice_tts_text_ganti_simbol():
    v = _load_voice()
    assert v._tts_text("Diskon 50% + ongkir 5&5") == \
        "Diskon 50 persen plus ongkir 5 dan 5"


def test_voice_stt_whisper_ok_boolean():
    v = _load_voice()
    assert isinstance(v._stt_whisper_ok(), bool)


def test_voice_speak_chain_default():
    v = _load_voice()
    assert v.DEFAULT_STT_MODEL == "base"
    assert v.speak.__defaults__[-2] == "id"


def test_voice_detect_voice_command():
    v = _load_voice()
    assert v._detect_voice_command("pakai suara cowok") == {"voice": "male"}
    assert v._detect_voice_command("ganti suara jadi cewek")["voice"] == "female"
    assert v._detect_voice_command("suara anak kecil")["voice"] == "child"
    assert v._detect_voice_command("bicara lebih cepat")["rate"] > 1.0
    assert v._detect_voice_command("kamu bicara lambat")["rate"] < 1.0
    assert v._detect_voice_command("suara serak")["pitch"] < 1.0
    assert v._detect_voice_command("suara melengking")["pitch"] > 1.0
    assert v._detect_voice_command("balik normal") == {"reset": True}
    assert v._detect_voice_command("oke lanjut") == {}


def test_voice_emotion_from_text():
    v = _load_voice()
    assert v._emotion_from_text("wkwkwk lucu banget") == "tertawa"
    assert v._emotion_from_text("aku sedih banget huhu") == "sedih"
    assert v._emotion_from_text("hore mantap!") == "ceria"
    assert v._emotion_from_text("goblok kamu sial") == "marah"
    assert v._emotion_from_text("apa kabar") == "netral"


def test_voice_emotion_from_audio():
    v = _load_voice()
    assert v._emotion_from_audio({"rms_db": -18, "f0_mean": 210, "f0_std": 55}) == "ceria"
    assert v._emotion_from_audio({"rms_db": -45, "f0_mean": 110, "f0_std": 12}) == "sedih"
    assert v._emotion_from_audio({"rms_db": -22, "f0_mean": 175, "f0_std": 10}) == "tegas"
    assert v._emotion_from_audio(None) == "netral"
    assert v._emotion_from_audio({"rms_db": -35, "f0_mean": 140, "f0_std": 30}) == "netral"


def test_voice_tts_rate_for_user():
    v = _load_voice()
    assert v._tts_rate_for_user(None) == 1.0
    assert abs(v._tts_rate_for_user(12.0) - 1.0) < 1e-6
    assert v._tts_rate_for_user(4.0) == 0.7
    assert v._tts_rate_for_user(18.0) == 1.35


def test_voice_speech_rate_est():
    v = _load_voice()
    assert v._speech_rate_est("halo apa kabar", {"dur": 2.0}) == 7.0
    assert v._speech_rate_est("halo apa kabar", None, 5) == 2.8
    assert v._speech_rate_est("halo", None, None) is None


def test_voice_profile_ema(tmp_path, monkeypatch):
    v = _load_voice()
    monkeypatch.setattr(v, "PROFILE_PATH", str(tmp_path / "vp.json"))
    v._profile_update("chars_per_sec", 8.0)
    v._profile_update("chars_per_sec", 10.0)
    p = v._load_profile()
    assert abs(p["chars_per_sec"] - 8.7) < 1e-3


def test_voice_speak_returns_tuple(monkeypatch):
    v = _load_voice()
    monkeypatch.setattr(v, "_synth_google", lambda t, p, l: (None, "no net"))
    monkeypatch.setattr(v, "_synth_mp3", lambda t, p, vc, r: (None, "no edge"))
    msg, interrupted = v.speak("halo", engine="auto", barge_in=False)
    assert isinstance(interrupted, bool)
    assert isinstance(msg, str)


def test_voice_play_mp3_returns_tuple(monkeypatch):
    v = _load_voice()
    monkeypatch.setattr(v, "_which", lambda name: None)
    err, interrupted = v._play_mp3("/tmp/x.mp3")
    assert "tidak ada" in err
    assert interrupted is False
