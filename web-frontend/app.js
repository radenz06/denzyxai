// denzyx AI — frontend member (register/login/status)
// Backend URL di-override file api.js (atau default localhost untuk dev).
var API = window.API_BASE || "";

function $(id) { return document.getElementById(id); }

function show(which) {
  $("login").classList.toggle("hidden", which !== "login");
  $("register").classList.toggle("hidden", which !== "register");
  $("panel").classList.toggle("hidden", which !== "panel");
}

function showLogin() { show("login"); }
function showReg() { show("register"); }

function setMsg(el, kind, text) {
  var m = $(el);
  if (!text) { m.className = "msg hidden"; m.innerHTML = ""; return; }
  m.className = "msg " + kind;
  m.innerHTML = text;
}

function esc(s) {
  return String(s == null ? "" : s).replace(/[&<>"]/g, function (c) {
    return { "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;" }[c];
  });
}

function api(path, data) {
  var opts = { method: "POST", headers: { "Content-Type": "application/json",
    "Accept": "application/json" } };
  if (data) opts.body = JSON.stringify(data);
  return fetch(API + path, opts).then(function (r) {
    return r.json().catch(function () { return { ok: false, error: "respons tidak valid" }; });
  });
}

function tgLink(j) {
  if (!j || !j.pay_link) return "";
  try {
    var m = j.pay_link.match(/href="([^"]+)"/);
    return m ? m[1] : "";
  } catch (e) { return ""; }
}

function doRegister() {
  var user = $("reg-user").value.trim(), pass = $("reg-pass").value,
      name = $("reg-name").value.trim();
  setMsg("reg-msg", "", "");
  if (user.length < 3 || pass.length < 4) {
    setMsg("reg-msg", "err", "Username min 3 karakter, password min 4 karakter.");
    return;
  }
  $("reg-btn").disabled = true;
  api("/api/register", { username: user, password: pass, display_name: name })
    .then(function (j) {
      if (j.ok) {
        var link = tgLink(j);
        setMsg("reg-msg", "ok", "Berhasil daftar, " + esc(j.username) + ".<br>"
          + "<b>Langkah aktivasi:</b><br>1. Chat bot Telegram untuk minta QR<br>"
          + "2. Bayar, kirim bukti, lalu akun diaktifkan.<br>"
          + (link ? '<a class="paybtn" target="_blank" rel="noopener" href="'
            + esc(link) + '">💬 Chat Bot untuk QR</a>' : ""));
        if (link) {
          // arahkan langsung ke bot setelah daftar
          setTimeout(function () { location.href = link; }, 1200);
        }
      } else {
        setMsg("reg-msg", "err", esc(j.error || "Gagal daftar."));
      }
    })
    .catch(function (e) { setMsg("reg-msg", "err", "Jaringan error: " + e); })
    .finally(function () { $("reg-btn").disabled = false; });
}

function doLogin() {
  var user = $("login-user").value.trim(), pass = $("login-pass").value;
  setMsg("login-msg", "", "");
  $("login-btn").disabled = true;
  api("/api/login", { username: user, password: pass })
    .then(function (j) {
      if (j.ok) {
        try { localStorage.setItem("denz_token", j.token); } catch (e) {}
        showPanel(j);
      } else if (j.error) {
        var link = tgLink(j);
        var extra = link ? '<br><a class="paybtn" target="_blank" rel="noopener" href="'
          + esc(link) + '">💬 Chat Bot untuk QR</a>' : "";
        setMsg("login-msg", "err", esc(j.error) + extra);
      }
    })
    .catch(function (e) { setMsg("login-msg", "err", "Jaringan error: " + e); })
    .finally(function () { $("login-btn").disabled = false; });
}

function showPanel(j) {
  var st = j.status || "";
  var badge = '<span class="badge ' + esc(st) + '">' + esc(st) + "</span>";
  var extra = "";
  if (st === "pending" && j.pay_link) {
    extra = '<a class="paybtn" target="_blank" rel="noopener" href="'
      + esc(tgLink(j)) + '">💬 Chat Bot untuk QR</a>';
  }
  $("panel-msg").innerHTML =
    '<div class="status-line">Username: <b>' + esc(j.username) + "</b> " + badge + "</div>"
    + '<div class="status-line">Aktif s/d: <b>' + esc(j.expires_at || "-") + "</b></div>"
    + extra
    + '<div class="switch" style="margin-top:14px"><a href="#" onclick="logout();return false">Keluar</a></div>';
  show("panel");
}

function logout() {
  try { localStorage.removeItem("denz_token"); } catch (e) {}
  location.reload();
}

// auto: kalau sudah punya token, cek status
window.addEventListener("load", function () {
  var tok = "";
  try { tok = localStorage.getItem("denz_token") || ""; } catch (e) {}
  if (!tok) return;
  fetch(API + "/api/me", { headers: { "Authorization": "Bearer " + tok,
    "Accept": "application/json" } })
    .then(function (r) { return r.json(); })
    .then(function (j) { if (j.ok) showPanel(j); })
    .catch(function () {});
});
