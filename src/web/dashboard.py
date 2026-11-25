"""Control dashboard (``/``)."""
from __future__ import annotations

from src.web.ui import render_page

_BODY = r"""
<div class="page-head">
  <div>
    <h1>控制台</h1>
    <div class="page-sub">6 币 5m Rebound（15m 默认关闭）· 前 60s 入场 · 入场后 TP/SL，无时间强平</div>
  </div>
  <div class="head-meta">
    模式 <b id="mode">--</b> · 配置版本 <b id="cfgver">--</b><br>
    <span id="uptime">--</span>
  </div>
</div>

<div class="stats-row stats-row-4">
  <div class="stat-card">
    <div class="stat-label">今日净 PnL (UTC)</div>
    <div class="stat-value" id="today-pnl">--</div>
    <div class="stat-sub" id="today-sub">--</div>
  </div>
  <div class="stat-card">
    <div class="stat-label">累计净 PnL</div>
    <div class="stat-value" id="total-pnl">--</div>
    <div class="stat-sub" id="total-sub">--</div>
  </div>
  <div class="stat-card">
    <div class="stat-label">胜率</div>
    <div class="stat-value purple" id="win-rate">--</div>
    <div class="stat-sub" id="win-sub">--</div>
  </div>
  <div class="stat-card">
    <div class="stat-label">盈亏比</div>
    <div class="stat-value orange" id="profit-factor">--</div>
    <div class="stat-sub" id="pf-sub">--</div>
  </div>
</div>

<div class="clock-bar">
  <div class="clock-item">
    <span class="clock-label">系统时间 UTC</span>
    <b id="sys-clock">--:--:--</b>
    <span class="clock-sub" id="local-clock">本地 --:--:--</span>
  </div>
  <div id="window-clocks" style="display:contents"></div>
</div>

<div class="control-bar">
  <div class="control-status">
    <span class="control-label">交易控制</span>
    <span class="control-meta" id="mode-note">--</span>
  </div>
  <div class="controls" style="margin:0">
    <button id="pause" class="btn btn-ghost">暂停新开仓</button>
    <button id="flatten" class="btn btn-danger">全部市价平仓</button>
    <a href="/config"><button type="button" class="btn btn-ghost">策略配置</button></a>
    <a href="/trades"><button type="button" class="btn btn-ghost">全部交易</button></a>
    <span class="msg" id="msg"></span>
  </div>
</div>

<div class="section">
  <h2>资产 / 当前窗口 <a href="/config">策略配置 →</a></h2>
  <div class="table-wrap">
    <table>
      <thead><tr>
        <th>市场</th><th>启用</th><th>窗口</th><th>阶段</th><th>方向</th><th>状态</th>
        <th class="num">mid</th><th class="num">bid/ask</th>
        <th class="num">entry≤</th><th class="num">TP</th><th class="num">止损</th>
        <th class="num">持仓</th><th class="num">浮盈 U</th>
      </tr></thead>
      <tbody id="assets"><tr><td colspan="13" class="no-data">加载中...</td></tr></tbody>
    </table>
  </div>
</div>

<div class="section">
  <h2>最近交易 <a href="/trades">全部 →</a></h2>
  <div class="table-wrap">
    <table>
      <thead><tr>
        <th>时间</th><th>资产</th><th>方向</th><th>原因</th>
        <th class="num">入场</th><th class="num">出场</th>
        <th class="num">净 PnL U</th>
      </tr></thead>
      <tbody id="trades"><tr><td colspan="7" class="no-data">加载中...</td></tr></tbody>
    </table>
  </div>
</div>
"""

_SCRIPT = r"""
let STATE = null;
// Server clock minus browser clock, refreshed with every /api/status call.
let SERVER_OFFSET_MS = 0;
let CLOCK_INTERVALS = [];
let CLOCK_KEY = '';

const fmtUtcHms = (d) => String(d.getUTCHours()).padStart(2, '0') + ':'
  + String(d.getUTCMinutes()).padStart(2, '0') + ':'
  + String(d.getUTCSeconds()).padStart(2, '0');
const fmtLocalHms = (d) => String(d.getHours()).padStart(2, '0') + ':'
  + String(d.getMinutes()).padStart(2, '0') + ':'
  + String(d.getSeconds()).padStart(2, '0');

function ensureWindowClocks() {
  const list = (STATE && STATE.intervals) || [];
  const key = JSON.stringify(list.map((i) => [i.name, i.window_sec]));
  if (key === CLOCK_KEY) return;
  CLOCK_KEY = key;
  CLOCK_INTERVALS = list;
  $('window-clocks').innerHTML = list.map((it, i) => `
    <div class="clock-item">
      <span class="clock-label">${escapeHtml(it.name)} 窗口 本地</span>
      <b id="win-range-${i}">--:-- – --:--</b>
    </div>
    <div class="clock-item">
      <span class="clock-label">${escapeHtml(it.name)} 本窗剩余</span>
      <b id="win-left-${i}" class="orange">--:--</b>
    </div>
    <div class="clock-progress"><div id="win-fill-${i}"></div></div>`).join('');
}

function tickWindowClock() {
  ensureWindowClocks();
  const serverMs = Date.now() + SERVER_OFFSET_MS;
  const serverDate = new Date(serverMs);
  const localDate = new Date();
  const nowS = serverMs / 1000;

  const sc = $('sys-clock');
  if (sc) sc.textContent = fmtUtcHms(serverDate);
  const lc = $('local-clock');
  if (lc) lc.textContent = '本地 ' + fmtLocalHms(localDate);

  CLOCK_INTERVALS.forEach((it, i) => {
    const span = Number(it.window_sec) || 300;
    const start = Math.floor(nowS / span) * span;
    const end = start + span;
    const left = Math.max(0, end - nowS);
    const range = $(`win-range-${i}`);
    if (range) range.textContent = fmtLocalHms(new Date(start * 1000))
      + ' – ' + fmtLocalHms(new Date(end * 1000));
    const wl = $(`win-left-${i}`);
    if (wl) wl.textContent = String(Math.floor(left / 60)).padStart(2, '0')
      + ':' + String(Math.floor(left % 60)).padStart(2, '0');
    const fill = $(`win-fill-${i}`);
    if (fill) fill.style.width = (((nowS - start) / span) * 100).toFixed(1) + '%';
  });
}
setInterval(tickWindowClock, 1000);
tickWindowClock();

function renderAssets(s) {
  const rows = [];
  for (const [market, a] of Object.entries(s.assets || {})) {
    const enabled = !!a.enabled;
    const enabledPill = enabled
      ? '<span class="pill green">启用</span>'
      : '<span class="pill">关闭</span>';
    const perf = (s.asset_perf || {})[market];
    const perfTag = perf && perf.blocked
      ? ' <span class="pill red" title="滚动表现触底，暂停开仓">表现停</span>'
      : (perf && Number(perf.stake_mult) < 1
        ? ` <span class="pill orange" title="滚动表现降权">×${perf.stake_mult}</span>`
        : '');
    const label = `${escapeHtml(a.asset || market)} <span class="pill blue">${escapeHtml(a.interval || '')}</span>`;
    const w = a.window || {};
    const sides = Object.entries(a.sides || {});
    if (sides.length === 0) {
      const reason = enabled
        ? `等待 ${escapeHtml(a.interval || '')} 窗口...`
        : '策略已关闭，无新开仓';
      rows.push(`<tr>
        <td><b>${label}</b>${perfTag}</td>
        <td>${enabledPill}</td>
        <td colspan="11" class="muted">${reason}</td>
      </tr>`);
      continue;
    }
    for (const [sideKey, side] of sides) {
      const p = side.position;
      const pos = p
        ? `${fmt(p.shares - p.sold_shares, 2)} @ ${fmt(p.entry_price, 3)}`
        : '--';
      rows.push(`<tr>
        <td><b>${label}</b>${perfTag}</td>
        <td>${enabledPill}</td>
        <td>${escapeHtml(w.slug || '--')}<br><span class="muted">${utcTime(w.start_ts)}</span></td>
        <td>${escapeHtml(side.phase || '--')}</td>
        <td>${escapeHtml(sideKey)}</td>
        <td>${escapeHtml(side.status || '--')}</td>
        <td class="num">${fmt(side.mid, 3)}</td>
        <td class="num">${fmt(side.bid, 3)} / ${fmt(side.ask, 3)}</td>
        <td class="num">${fmt(side.entry_thresh, 2)}</td>
        <td class="num">${fmt(side.tp_pct * 100, 0)}%</td>
        <td class="num">${fmt(side.stop_loss_pct * 100, 0)}%</td>
        <td class="num">${pos}</td>
        <td class="num ${cls(side.unrealized_usd)}">${money(side.unrealized_usd, 3)}</td>
      </tr>`);
    }
  }
  $('assets').innerHTML = rows.join('') || '<tr><td colspan="13" class="no-data">暂无市场</td></tr>';
}

function renderTrades(trades) {
  const rows = (trades || []).map((r) => `<tr>
    <td>${utcFull(r.exit_ts)}</td>
    <td><b>${escapeHtml(r.asset)}</b>
      <span class="pill blue">${escapeHtml(r.interval || '5m')}</span></td>
    <td>${escapeHtml(r.side)}</td>
    <td>${escapeHtml(r.reason)}</td>
    <td class="num">${fmt(r.entry_price, 3)}</td>
    <td class="num">${fmt(r.exit_price, 3)}</td>
    <td class="num ${cls(r.pnl_net)}">${money(r.pnl_net, 4)}</td>
  </tr>`);
  $('trades').innerHTML = rows.join('') || '<tr><td colspan="7" class="no-data">暂无交易</td></tr>';
}

function renderStats(stats) {
  if (!stats) return;
  const t = stats.today || {};
  const a = stats.total || {};
  $('today-pnl').textContent = money(t.net, 4);
  $('today-pnl').className = 'stat-value ' + cls(t.net);
  $('today-sub').textContent = `${t.trades || 0} 笔 · ${t.wins || 0} 胜 / ${t.losses || 0} 负`;
  $('total-pnl').textContent = money(a.net, 4);
  $('total-pnl').className = 'stat-value ' + cls(a.net);
  $('total-sub').textContent = `${a.trades || 0} 笔 · 毛 ${fmt(a.gross, 2)} / 费 ${fmt(a.fees, 2)}`;
  $('win-rate').textContent = pct(a.win_rate);
  $('win-sub').textContent = `${a.wins || 0} 胜 / ${a.losses || 0} 负`;
  $('profit-factor').textContent = a.profit_factor === null
    ? (a.wins ? '∞' : '--') : fmt(a.profit_factor, 2);
  $('pf-sub').textContent = `均赢 ${money(a.avg_win, 2)} · 均亏 ${money(a.avg_loss, 2)}`;
}

async function refresh() {
  try {
    const s = await getJSON('/api/status');
    STATE = s;
    if (s.ts) SERVER_OFFSET_MS = Number(s.ts) * 1000 - Date.now();
    tickWindowClock();
    $('mode').textContent = s.mode === 'live' ? '实盘' : '纸面';
    $('mode').className = s.mode === 'live' ? 'red' : 'green';
    $('cfgver').textContent = 'v' + (s.config_version ?? 0);
    $('uptime').textContent = '运行 ' + Math.round(s.uptime_s || 0) + ' 秒';
    $('pause').textContent = s.paused ? '恢复新开仓' : '暂停新开仓';
    const ss = s.same_side_open || {};
    const sideLimit = s.max_same_side_positions ?? '--';
    const winLimit = s.max_same_side_per_window ?? '--';
    const exposure = Object.entries(s.window_exposure || {}).map(
      ([name, e]) => `${name} ${e.positions ?? 0}/${s.max_positions_per_window ?? '--'}笔`
        + ` ${money(e.notional_usd, 2)}/${s.max_window_notional_usd ?? '--'}U`
    ).join(' · ');
    const open = s.open_exposure || {};
    $('mode-note').textContent = (s.mode === 'live'
      ? '实盘模式：真实 CLOB FAK 订单'
      : '纸面模式：实时订单簿模拟成交，已扣官方 taker 费（BTC 5% / 其他 1%）')
      + ` · 同向 UP ${ss.UP ?? 0}/${sideLimit} · DOWN ${ss.DOWN ?? 0}/${sideLimit}`
      + ` · 单窗同向 ≤${winLimit}`
      + (exposure ? ` · 单窗 ${exposure}` : '')
      + ` · 全局 ${open.count ?? 0}/${s.max_open_positions ?? '--'}笔`
      + ` ${money(open.notional_usd, 2)}/${s.max_open_notional_usd ?? '--'}U`
      + ` · 回撤 ${money(s.intraday_drawdown_usd, 2)}/${s.max_intraday_drawdown_usd ?? '--'}U`
      + ((s.drawdown_pause_remaining_s || 0) > 0
        ? `（暂停 ${Math.ceil(s.drawdown_pause_remaining_s / 60)}m）` : '');
    $('mode-note').className = 'control-meta ' + (s.mode === 'live' ? 'red' : 'muted');
    renderAssets(s);

    const [stats, trades] = await Promise.all([
      getJSON('/api/stats'),
      getJSON('/api/trades?limit=15'),
    ]);
    renderStats(stats);
    renderTrades(trades.trades || []);
    $('msg').textContent = '';
  } catch (e) {
    $('msg').textContent = String(e);
    $('msg').className = 'msg red';
  }
}

$('pause').onclick = async () => {
  const next = !(STATE && STATE.paused);
  try {
    await postJSON('/api/pause', {paused: next, reason: next ? 'dashboard' : ''});
  } catch (e) {
    $('msg').textContent = String(e);
    $('msg').className = 'msg red';
  }
  refresh();
};
$('flatten').onclick = async () => {
  const c = prompt('这会以市价卖出所有未平仓仓位。输入 FLATTEN 确认：');
  if (c !== 'FLATTEN') return;
  try {
    const r = await postJSON('/api/flatten', {confirm: c});
    $('msg').textContent = '已尝试平仓 ' + (r.sold ?? 0) + ' 笔';
    $('msg').className = 'msg orange';
  } catch (e) {
    $('msg').textContent = String(e);
    $('msg').className = 'msg red';
  }
  refresh();
};

refresh();
setInterval(refresh, 2000);
"""

INDEX_HTML = render_page("控制台", "/", _BODY, _SCRIPT)
