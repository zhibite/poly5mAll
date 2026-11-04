"""Shared page shell, CSS and tiny JS helpers for the backend UI.

Visual language follows ``02.poly1h/src/web/static/style.css``: near-black
indigo background, purple accent, compact stat cards and table sections.
"""
from __future__ import annotations

STYLE = r"""
* { box-sizing:border-box; margin:0; padding:0; }
body { font-family:-apple-system,BlinkMacSystemFont,"Segoe UI",
       "Microsoft YaHei",Roboto,sans-serif; background:#0a0a14; color:#e0e0e0; }
a { color:#a78bfa; }
nav { background:#12122a; border-bottom:1px solid #1e1e3a; padding:0 24px;
      display:flex; align-items:center; gap:8px; height:56px;
      position:sticky; top:0; z-index:20; }
nav a { color:#a0a0b0; text-decoration:none; padding:8px 16px;
        border-radius:6px; font-size:14px; }
nav a:hover, nav a.active { background:#1e1e3a; color:#a78bfa; }
nav .brand { font-weight:700; font-size:17px; color:#7c3aed;
             margin-right:16px; letter-spacing:-.02em; }
nav .nav-right { margin-left:auto; display:flex; align-items:center;
                 gap:9px; font-size:12px; color:#6b7280; }
nav .nav-right a { padding:4px 8px; font-size:12px; color:#a78bfa; }
nav .nav-user { color:#9ca3af; }
.live-dot { display:inline-block; width:8px; height:8px; border-radius:50%;
            background:#10b981; animation:pulse 2s infinite; }
.live-dot.offline { background:#ef4444; animation:none; }
@keyframes pulse { 0%,100%{opacity:1} 50%{opacity:.4} }
.container { max-width:1200px; margin:0 auto; padding:24px; }

.stats-row { display:grid; grid-template-columns:repeat(auto-fit,minmax(190px,1fr));
             gap:16px; margin-bottom:24px; }
.stat-card { background:#12122a; border:1px solid #1e1e3a; border-radius:10px;
             padding:20px; }
.stat-label { font-size:12px; color:#6b7280; text-transform:uppercase;
              letter-spacing:.05em; margin-bottom:8px; }
.stat-value { font-size:26px; font-weight:700; font-variant-numeric:tabular-nums; }
.stat-sub { font-size:12px; color:#6b7280; margin-top:4px; }
.green { color:#10b981; } .red { color:#ef4444; }
.purple { color:#a78bfa; } .orange { color:#fb923c; } .muted { color:#6b7280; }

.section { background:#12122a; border:1px solid #1e1e3a; border-radius:10px;
           padding:20px; margin-bottom:24px; }
.section h2 { font-size:15px; font-weight:600; margin-bottom:14px;
              color:#d1d5db; display:flex; align-items:center; gap:8px; }
.section h2 a { text-transform:none; letter-spacing:0; font-size:12px;
                margin-left:auto; }
table { width:100%; border-collapse:collapse; font-size:13px; }
th { text-align:left; color:#6b7280; font-weight:500; font-size:11px;
     text-transform:uppercase; letter-spacing:.05em; padding:8px 10px;
     border-bottom:1px solid #1e1e3a; white-space:nowrap; }
td { padding:10px; border-bottom:1px solid #171730; color:#c9c9d6;
     white-space:nowrap; font-variant-numeric:tabular-nums; }
td.num, th.num { text-align:right; }
tr:last-child td { border-bottom:none; }
tbody tr:hover { background:#171730; }
.no-data { text-align:center; color:#4b5563; padding:20px; }
.mono { font-family:ui-monospace,SFMono-Regular,Consolas,monospace; }
.table-wrap { width:100%; overflow-x:auto; -webkit-overflow-scrolling:touch; }

.pill { font-size:11px; padding:2px 7px; border-radius:4px; background:#1e1e3a;
        color:#a0a0b0; display:inline-block; }
.pill.green { background:#064e3b; color:#34d399; }
.pill.red { background:#450a0a; color:#f87171; }
.pill.blue { background:#1e1b4b; color:#a78bfa; }
.pill.orange { background:#3a2f10; color:#fbbf24; }
.badge { font-size:11px; padding:2px 8px; border-radius:999px; font-weight:600; }
.badge.green { background:#064e3b; color:#34d399; }
.badge.red { background:#450a0a; color:#f87171; }
.badge.blue { background:#1e1b4b; color:#a78bfa; }

.page-head { display:flex; align-items:flex-end; justify-content:space-between;
             gap:16px; flex-wrap:wrap; margin-bottom:18px; }
.page-head h1 { font-size:22px; font-weight:700; letter-spacing:-.01em; }
.page-sub { font-size:12px; color:#6b7280; margin-top:4px; }
.head-meta { font-size:12px; color:#6b7280; text-align:right; }
.head-meta b { color:#a78bfa; font-weight:600; }

.controls { display:flex; align-items:center; gap:10px; flex-wrap:wrap;
            margin-bottom:18px; }
.control-bar { display:flex; align-items:center; gap:12px; flex-wrap:wrap;
               background:#12122a; border:1px solid #1e1e3a; border-radius:12px;
               padding:14px 18px; margin-bottom:20px; }
.control-status { display:flex; align-items:center; gap:10px; flex-wrap:wrap; }
.control-label { font-size:14px; font-weight:600; }
.control-meta { font-size:12px; color:#6b7280; }
button.btn { border:none; border-radius:7px; padding:9px 16px; font-size:13px;
             font-weight:600; cursor:pointer; transition:filter .15s; }
button.btn:hover { filter:brightness(1.15); }
button.btn:disabled { opacity:.45; cursor:default; filter:none; }
.btn-primary { background:#7c3aed; color:#fff; }
.btn-ghost { background:#1e1e3a; color:#a0a0b0; }
.btn-danger { background:#dc2626; color:#fff; }
.btn-sm { padding:6px 12px; font-size:12px; }
a > button { pointer-events:none; }
select, input[type="number"], input[type="text"], input[type="password"] {
  background:#0a0a18; color:#e5e7eb; border:1px solid #2a2a50;
  border-radius:8px; padding:8px 10px; font-size:13px; outline:none;
  font-variant-numeric:tabular-nums;
}
select:focus, input:focus { border-color:#7c3aed;
  box-shadow:0 0 0 3px rgba(124,58,237,.16); }
input[type="checkbox"] { width:18px; height:18px; accent-color:#7c3aed; }

.alert { border-radius:8px; padding:12px 16px; margin-bottom:18px;
         font-size:13px; line-height:1.55; }
.alert.warn { background:#3a1d10; border:1px solid #7c2d12; color:#fdba74; }
.alert.info { background:#101a2e; border:1px solid #1e3a5f; color:#93c5fd; }
.alert.ok { background:#062e22; border:1px solid #065f46; color:#6ee7b7; }
.alert.err { background:#450a0a; border:1px solid #7f1d1d; color:#fca5a5; }
.msg { font-size:12px; color:#6b7280; }
.msg.green { color:#10b981; } .msg.red { color:#ef4444; }
.msg.orange { color:#fb923c; }

/* ── Config cards ───────────────────────────────────────────────────── */
.config-grid { display:grid; grid-template-columns:repeat(auto-fit,minmax(300px,1fr));
               gap:16px; margin-bottom:18px; }
.config-group, .asset-card { background:#12122a; border:1px solid #1e1e3a;
  border-radius:12px; padding:18px 20px 20px; margin-bottom:0; }
.config-group h2 { display:flex; align-items:center; gap:9px; font-size:14px;
  font-weight:600; color:#d1d5db; margin-bottom:14px; }
.config-group h2 .dot { width:8px; height:8px; border-radius:50%;
  background:linear-gradient(135deg,#a78bfa,#7c3aed);
  box-shadow:0 0 10px rgba(124,58,237,.5); }
.asset-head { display:flex; align-items:center; justify-content:space-between;
              gap:10px; margin-bottom:12px; }
.asset-name { font-size:18px; font-weight:700; letter-spacing:.02em; }
.asset-head .state { font-size:11px; }
.asset-fields { display:grid; grid-template-columns:1fr 1fr; gap:10px; }
.field { display:flex; flex-direction:column; gap:6px; background:#0a0a14;
         border:1px solid #1e1e3a; border-radius:8px; padding:12px;
         transition:border-color .15s, box-shadow .15s; }
.field:hover { border-color:#2a2a4a; }
.field label { font-size:11px; color:#9ca3af; display:flex; align-items:center;
               gap:8px; flex-wrap:wrap; }
.field input, .field select { width:100%; }
.field.changed { border-color:#7c3aed;
  box-shadow:0 0 0 1px rgba(124,58,237,.35),0 0 20px rgba(124,58,237,.12); }
.field .help { font-size:10px; color:#4b5563; line-height:1.45; }
.fee-line { margin-top:12px; padding-top:12px; border-top:1px solid #1e1e3a;
            font-size:12px; color:#9ca3af; line-height:1.7; }
.fee-line .fee-row { display:flex; align-items:center; gap:8px; flex-wrap:wrap; }
.switch-row { display:flex; align-items:center; gap:8px; font-size:12px;
              color:#9ca3af; }
.save-bar { position:sticky; bottom:14px; z-index:10; display:flex;
  align-items:center; gap:14px; padding:14px 18px;
  background:rgba(10,10,20,.92); backdrop-filter:blur(10px);
  border:1px solid #2a2a4a; border-radius:12px;
  box-shadow:0 12px 40px rgba(0,0,0,.5); margin-top:10px; }
.save-bar .msg { flex:1; }

/* ── Pager ──────────────────────────────────────────────────────────── */
.pager { display:flex; align-items:center; justify-content:flex-end;
         gap:10px; padding-top:14px; }
.pager-info { font-size:12px; color:#6b7280; }

/* ── Login ──────────────────────────────────────────────────────────── */
.login-body { min-height:100vh; display:flex; align-items:center;
  justify-content:center;
  background:radial-gradient(1200px 600px at 50% -10%,#171735 0%,#0a0a14 60%); }
.login-wrap { width:100%; max-width:390px; padding:24px; }
.login-card { background:#12122a; border:1px solid #1e1e3a; border-radius:12px;
  padding:30px; box-shadow:0 20px 60px rgba(0,0,0,.45); }
.login-brand { font-size:24px; font-weight:800; color:#7c3aed;
               letter-spacing:-.03em; }
.login-sub { font-size:13px; color:#6b7280; margin:4px 0 20px; }
.login-field { display:flex; flex-direction:column; gap:6px; font-size:12px;
               color:#9ca3af; margin-bottom:14px; }
.login-field input { background:#0a0a14; border:1px solid #2a2a4a;
  border-radius:7px; color:#e0e0e0; padding:11px 12px; font-size:14px;
  width:100%; }
.login-field input:focus { outline:none; border-color:#7c3aed;
  box-shadow:0 0 0 3px rgba(124,58,237,.16); }
.login-btn { width:100%; margin-top:6px; }
.login-foot { text-align:center; margin-top:16px; font-size:12px; }

footer { text-align:center; color:#4b5563; font-size:12px; padding:20px 0 40px; }

@media (max-width:760px) {
  .container { padding:14px; }
  nav { height:auto; min-height:52px; padding:8px 12px; flex-wrap:wrap; }
  nav .brand { margin-right:4px; font-size:15px; }
  nav a { padding:6px 9px; font-size:13px; }
  nav .nav-right { margin-left:auto; gap:6px; font-size:11px; }
  .stats-row { gap:10px; margin-bottom:16px; }
  .stat-card { padding:14px; }
  .stat-value { font-size:21px; }
  .stat-label { font-size:10px; }
  .section { padding:14px; margin-bottom:16px; }
  table { font-size:12px; }
  th { font-size:10px; padding:7px 8px; }
  td { padding:8px; }
  .page-head { flex-direction:column; align-items:flex-start; gap:8px; }
  .head-meta { text-align:left; }
  .asset-fields { grid-template-columns:1fr; }
  .save-bar { flex-wrap:wrap; }
}
"""

NAV_ITEMS = (
    ("/", "控制台"),
    ("/trades", "交易记录"),
    ("/config", "策略配置"),
)

_TEMPLATE = """<!doctype html>
<html lang="zh-CN">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>__TITLE__ · Poly5m All</title>
<style>__STYLE__</style>
</head>
<body>
<nav>
  <span class="brand">Poly5m</span>
  __NAV__
  <div class="nav-right">
    <span class="live-dot" id="live-dot"></span>
    <span id="live-label">连接中</span>
    <span class="mono" id="clock"></span>
    <span class="nav-user" id="nav-user"></span>
    <a href="#" id="logout" style="display:none">退出</a>
  </div>
</nav>
<div class="container">
__BODY__
</div>
<footer>Poly5m All · 7 币 5m Rebound · 后端 8033 · 表格时间按浏览器时区 · 统计按 UTC 日</footer>
<script>
const $ = (id) => document.getElementById(id);
const fmt = (v, d = 2) => (v === null || v === undefined || isNaN(Number(v)))
  ? '--' : Number(v).toFixed(d);
const money = (v, d = 4) => (v === null || v === undefined || isNaN(Number(v)))
  ? '--' : (Number(v) >= 0 ? '+' : '') + Number(v).toFixed(d) + ' U';
const pct = (v, d = 1) => (v === null || v === undefined || isNaN(Number(v)))
  ? '--' : (Number(v) * 100).toFixed(d) + '%';
const cls = (v) => Number(v) > 0 ? 'green' : Number(v) < 0 ? 'red' : 'muted';
const utcTime = (ts) => ts
  ? new Date(Number(ts) * 1000).toLocaleTimeString('zh-CN', {hour12:false})
  : '--';
const utcFull = (ts) => ts
  ? new Date(Number(ts) * 1000).toLocaleString('zh-CN', {hour12:false})
  : '--';
const escapeHtml = (v) => String(v ?? '').replace(/[&<>"']/g, (c) => (
  {'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]
));

async function getJSON(path) {
  const r = await fetch(path, {cache:'no-store'});
  if (r.status === 401) {
    location.href = '/login?next=' + encodeURIComponent(location.pathname);
    throw new Error('需要登录');
  }
  if (!r.ok) throw new Error(path + ' HTTP ' + r.status);
  return await r.json();
}
async function postJSON(path, body) {
  const r = await fetch(path, {
    method:'POST',
    headers:{'Content-Type':'application/json'},
    body: JSON.stringify(body || {}),
  });
  if (r.status === 401) {
    location.href = '/login?next=' + encodeURIComponent(location.pathname);
    throw new Error('需要登录');
  }
  if (!r.ok) {
    let detail = 'HTTP ' + r.status;
    try { const j = await r.json(); detail = j.detail || detail; } catch (e) {}
    throw new Error(detail);
  }
  return await r.json();
}
function setLive(ok) {
  const dot = $('live-dot'); const label = $('live-label');
  if (dot) dot.className = 'live-dot' + (ok ? '' : ' offline');
  if (label) label.textContent = ok ? '在线' : '离线';
}
function tickClock() {
  const el = $('clock');
  if (el) el.textContent = new Date().toISOString().slice(11,19) + 'Z';
}
setInterval(tickClock, 1000); tickClock();
async function ping() {
  try { await getJSON('/healthz'); setLive(true); }
  catch (e) { setLive(false); }
}
setInterval(ping, 5000); ping();

async function loadMe() {
  try {
    const me = await fetch('/api/auth/me', {cache:'no-store'}).then(r => r.json());
    const user = $('nav-user'); const logout = $('logout');
    if (me.auth_enabled) {
      if (user) user.textContent = me.username || '';
      if (logout) logout.style.display = '';
    } else {
      if (user) user.textContent = '开放访问';
      if (logout) logout.style.display = 'none';
    }
  } catch (e) { /* ignore */ }
}
if ($('logout')) {
  $('logout').addEventListener('click', async (ev) => {
    ev.preventDefault();
    try { await postJSON('/api/auth/logout', {}); } catch (e) {}
    location.href = '/login';
  });
}
loadMe();
</script>
<script>__SCRIPT__</script>
</body>
</html>
"""


def nav(active: str) -> str:
    parts: list[str] = []
    for href, label in NAV_ITEMS:
        cls = ' class="active"' if href == active else ""
        parts.append(f'<a href="{href}"{cls}>{label}</a>')
    return "".join(parts)


def render_page(title: str, active: str, body: str, script: str) -> str:
    return (
        _TEMPLATE.replace("__TITLE__", title)
        .replace("__STYLE__", STYLE)
        .replace("__NAV__", nav(active))
        .replace("__BODY__", body)
        .replace("__SCRIPT__", script)
    )


_LOGIN_TEMPLATE = """<!doctype html>
<html lang="zh-CN">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>登录 · Poly5m All</title>
<style>__STYLE__</style>
</head>
<body class="login-body">
<div class="login-wrap">
  <div class="login-card">
    <div class="login-brand">Poly5m</div>
    <div class="login-sub">7 币 5m 策略控制台</div>
    <div id="alert-box"></div>
    <form id="login-form" autocomplete="on">
      <label class="login-field">用户名
        <input id="username" name="username" autocomplete="username" value="admin" required>
      </label>
      <label class="login-field">密码
        <input id="password" name="password" type="password"
               autocomplete="current-password" required>
      </label>
      <button class="btn btn-primary login-btn" type="submit" id="login-btn">登录</button>
    </form>
    <div class="login-foot muted">登录后访问控制台、交易记录和策略配置</div>
  </div>
</div>
<script>
const $ = (id) => document.getElementById(id);
const params = new URLSearchParams(location.search);
function safeNext(value) {
  return (value && value.startsWith('/') && !value.startsWith('//')) ? value : '/';
}
const NEXT = safeNext(params.get('next'));
function showAlert(html, cls) {
  $('alert-box').innerHTML = html ? `<div class="alert ${cls}">${html}</div>` : '';
}
async function init() {
  try {
    const me = await fetch('/api/auth/me', {cache:'no-store'}).then(r => r.json());
    if (me.authenticated) { location.href = NEXT; return; }
    if (!me.auth_enabled) {
      showAlert('管理员尚未设置 <code>WEB_PASSWORD</code>，无需登录。' +
                '请到 Coolify 环境变量设置密码后重启。', 'warn');
      $('login-btn').disabled = true;
      $('username').disabled = true;
      $('password').disabled = true;
    }
  } catch (e) { /* form still works */ }
}
$('login-form').addEventListener('submit', async (ev) => {
  ev.preventDefault();
  showAlert('', '');
  $('login-btn').disabled = true;
  $('login-btn').textContent = '登录中...';
  try {
    const r = await fetch('/api/auth/login', {
      method:'POST',
      headers:{'Content-Type':'application/json'},
      body: JSON.stringify({
        username: $('username').value,
        password: $('password').value,
      }),
    });
    const body = await r.json().catch(() => ({}));
    if (!r.ok) throw new Error(body.detail || ('HTTP ' + r.status));
    location.href = NEXT;
  } catch (e) {
    showAlert(e.message, 'warn');
    $('login-btn').disabled = false;
    $('login-btn').textContent = '登录';
    $('password').focus();
  }
});
init();
</script>
</body>
</html>
"""


def render_login_page() -> str:
    return _LOGIN_TEMPLATE.replace("__STYLE__", STYLE)
