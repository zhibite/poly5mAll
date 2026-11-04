"""Shared page shell, CSS and tiny JS helpers for the backend UI."""
from __future__ import annotations

STYLE = r"""
:root {
  --bg:#0d1117; --panel:#161b22; --panel2:#12161c; --line:#30363d;
  --text:#e6edf3; --muted:#8b949e; --green:#3fb950; --red:#f85149;
  --accent:#58a6ff; --orange:#d29922; --purple:#bc8cff;
}
* { box-sizing:border-box; }
body { margin:0; background:var(--bg); color:var(--text);
       font:14px/1.45 -apple-system,BlinkMacSystemFont,"Segoe UI",
            "Microsoft YaHei",Roboto,sans-serif; }
nav { display:flex; align-items:center; gap:18px; padding:0 22px;
      height:52px; background:var(--panel2); border-bottom:1px solid var(--line);
      position:sticky; top:0; z-index:10; }
nav .brand { font-weight:700; font-size:16px; margin-right:8px; }
nav a { color:var(--muted); text-decoration:none; padding:16px 2px;
        border-bottom:2px solid transparent; }
nav a:hover { color:var(--text); }
nav a.active { color:var(--text); border-bottom-color:var(--accent); }
nav .nav-right { margin-left:auto; display:flex; align-items:center; gap:10px;
                 color:var(--muted); font-size:12px; }
.dot { width:8px; height:8px; border-radius:50%; background:var(--green);
       display:inline-block; }
.dot.offline { background:var(--red); }
.container { padding:20px 22px 36px; }
.page-head { display:flex; justify-content:space-between; align-items:flex-end;
             gap:16px; flex-wrap:wrap; margin-bottom:14px; }
h1 { font-size:21px; margin:0; }
h2 { font-size:13px; margin:20px 0 8px; color:var(--muted);
     text-transform:uppercase; letter-spacing:.06em; }
.page-sub { color:var(--muted); font-size:13px; margin-top:4px; }
.head-meta { color:var(--muted); font-size:12px; text-align:right; }
.cards { display:grid; grid-template-columns:repeat(auto-fit,minmax(150px,1fr));
         gap:10px; margin:12px 0 18px; }
.card { background:var(--panel); border:1px solid var(--line);
        border-radius:8px; padding:10px 12px; }
.card span { display:block; color:var(--muted); font-size:12px; }
.card strong { display:block; font-size:18px; margin-top:2px;
               font-variant-numeric:tabular-nums; }
.controls { display:flex; gap:10px; align-items:center; flex-wrap:wrap;
            margin:12px 0; }
button { background:#21262d; color:var(--text); border:1px solid var(--line);
         border-radius:6px; padding:7px 12px; cursor:pointer; font-size:13px; }
button:hover { border-color:var(--accent); }
button.primary { background:#1f6feb; border-color:#1f6feb; color:#fff; }
button.primary:hover { background:#2f81f7; }
button.danger { color:#ffb3ad; border-color:#6e2b2b; }
button.danger:hover { border-color:var(--red); }
button:disabled { opacity:.45; cursor:not-allowed; }
.msg { color:var(--muted); font-size:12px; }
.msg.green { color:var(--green); } .msg.red { color:var(--red); }
.msg.orange { color:var(--orange); }
.table-wrap { overflow-x:auto; border:1px solid var(--line); border-radius:8px; }
table { width:100%; border-collapse:collapse; background:var(--panel); }
th,td { padding:7px 9px; border-bottom:1px solid var(--line);
        text-align:left; white-space:nowrap; font-variant-numeric:tabular-nums; }
th { color:var(--muted); font-weight:500; font-size:12px; background:var(--panel2); }
tbody tr:last-child td { border-bottom:0; }
tbody tr:hover { background:#1b222c; }
td.num,th.num { text-align:right; }
.pos { color:var(--green); } .neg { color:var(--red); }
.muted { color:var(--muted); }
.mono { font-family:ui-monospace,SFMono-Regular,Consolas,monospace; }
.pill { display:inline-block; padding:1px 7px; border-radius:999px;
        border:1px solid var(--line); font-size:11px; color:var(--muted); }
.pill.green { color:var(--green); border-color:#235c33; }
.pill.red { color:var(--red); border-color:#6e2b2b; }
.pill.blue { color:var(--accent); border-color:#1f4a7a; }
.badge { display:inline-block; padding:1px 7px; border-radius:999px;
         font-size:11px; border:1px solid var(--line); }
.badge.red { color:var(--red); border-color:#6e2b2b; }
.badge.green { color:var(--green); border-color:#235c33; }
.alert { border:1px solid var(--line); border-left:3px solid var(--accent);
         background:var(--panel); border-radius:6px; padding:10px 12px;
         color:var(--muted); font-size:12px; margin:12px 0; }
.alert.warn { border-left-color:var(--orange); }
.alert.err { border-left-color:var(--red); }
.field { background:var(--panel); border:1px solid var(--line);
         border-radius:8px; padding:10px; }
.field label { display:block; color:var(--muted); font-size:12px; margin-bottom:5px; }
.field input, .field select, select, input[type="number"] {
  background:#0d1117; color:var(--text); border:1px solid var(--line);
  border-radius:5px; padding:6px 8px; font-size:13px; width:100%;
}
.field input:focus, select:focus { outline:none; border-color:var(--accent); }
.config-grid { display:grid; grid-template-columns:repeat(auto-fit,minmax(180px,1fr));
               gap:10px; }
.config-group { margin-bottom:18px; }
.config-group h2 { display:flex; align-items:center; gap:8px; }
.row-changed { background:#1c2533 !important; }
.pager { display:flex; align-items:center; gap:12px; margin-top:10px; }
.pager-info { color:var(--muted); font-size:12px; }
.no-data { color:var(--muted); text-align:center; padding:18px; }
footer { color:var(--muted); font-size:11px; padding:18px 22px 30px;
         border-top:1px solid var(--line); margin-top:10px; }
input[type="checkbox"] { width:auto; accent-color:var(--accent); }
@media (max-width:900px) {
  nav { padding:0 10px; gap:10px; overflow-x:auto; }
  .container { padding:12px 10px 28px; }
  .head-meta { text-align:left; }
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
  <span class="brand">Poly5m All</span>
  __NAV__
  <div class="nav-right">
    <span class="dot" id="live-dot"></span>
    <span id="live-label">连接中</span>
    <span class="mono" id="clock"></span>
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
const cls = (v) => Number(v) > 0 ? 'pos' : Number(v) < 0 ? 'neg' : 'muted';
const utcTime = (ts) => ts
  ? new Date(Number(ts) * 1000).toLocaleTimeString('zh-CN', {hour12: false})
  : '--';
const utcFull = (ts) => ts
  ? new Date(Number(ts) * 1000).toLocaleString('zh-CN', {hour12: false})
  : '--';
const escapeHtml = (v) => String(v ?? '').replace(/[&<>"']/g, (c) => (
  {'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]
));

async function getJSON(path) {
  const r = await fetch(path, {cache: 'no-store'});
  if (!r.ok) throw new Error(path + ' HTTP ' + r.status);
  return await r.json();
}
function setLive(ok) {
  const dot = $('live-dot');
  const label = $('live-label');
  if (dot) dot.className = 'dot' + (ok ? '' : ' offline');
  if (label) label.textContent = ok ? '在线' : '离线';
}
function tickClock() {
  const el = $('clock');
  if (el) el.textContent = new Date().toISOString().slice(11, 19) + 'Z';
}
setInterval(tickClock, 1000); tickClock();
async function ping() {
  try { await getJSON('/healthz'); setLive(true); }
  catch (e) { setLive(false); }
}
setInterval(ping, 5000); ping();
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
