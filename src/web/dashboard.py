"""Control dashboard (``/``)."""
from __future__ import annotations

from src.web.ui import render_page

_BODY = r"""
<div class="page-head">
  <div>
    <h1>控制台</h1>
    <div class="page-sub">7 币 5m Rebound · 0–60s 入场 / 60–240s TP / 240s 强平</div>
  </div>
  <div class="head-meta">
    模式 <b id="mode">--</b> · 配置版本 <b id="cfgver">--</b><br>
    <span id="uptime">--</span>
  </div>
</div>

<div class="stats-row">
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
  <div class="stat-card">
    <div class="stat-label">新开仓</div>
    <div class="stat-value" id="entries">--</div>
    <div class="stat-sub" id="entries-sub">--</div>
  </div>
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
        <th>资产</th><th>启用</th><th>窗口</th><th>阶段</th><th>方向</th><th>状态</th>
        <th class="num">mid</th><th class="num">bid/ask</th>
        <th class="num">entry≤</th><th class="num">TP</th>
        <th class="num">持仓</th><th class="num">浮盈 U</th>
      </tr></thead>
      <tbody id="assets"><tr><td colspan="12" class="no-data">加载中...</td></tr></tbody>
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

function renderAssets(s) {
  const rows = [];
  for (const [asset, a] of Object.entries(s.assets || {})) {
    const enabled = !!a.enabled;
    const enabledPill = enabled
      ? '<span class="pill green">启用</span>'
      : '<span class="pill">关闭</span>';
    const w = a.window || {};
    const sides = Object.entries(a.sides || {});
    if (sides.length === 0) {
      const reason = enabled ? '等待 5m 窗口...' : '策略已关闭，无新开仓';
      rows.push(`<tr>
        <td><b>${escapeHtml(asset)}</b></td>
        <td>${enabledPill}</td>
        <td colspan="10" class="muted">${reason}</td>
      </tr>`);
      continue;
    }
    for (const [sideKey, side] of sides) {
      const p = side.position;
      const pos = p
        ? `${fmt(p.shares - p.sold_shares, 2)} @ ${fmt(p.entry_price, 3)}`
        : '--';
      rows.push(`<tr>
        <td><b>${escapeHtml(asset)}</b></td>
        <td>${enabledPill}</td>
        <td>${escapeHtml(w.slug || '--')}<br><span class="muted">${utcTime(w.start_ts)}</span></td>
        <td>${escapeHtml(side.phase || '--')}</td>
        <td>${escapeHtml(sideKey)}</td>
        <td>${escapeHtml(side.status || '--')}</td>
        <td class="num">${fmt(side.mid, 3)}</td>
        <td class="num">${fmt(side.bid, 3)} / ${fmt(side.ask, 3)}</td>
        <td class="num">${fmt(side.entry_thresh, 2)}</td>
        <td class="num">${fmt(side.tp_pct * 100, 0)}%</td>
        <td class="num">${pos}</td>
        <td class="num ${cls(side.unrealized_usd)}">${money(side.unrealized_usd, 3)}</td>
      </tr>`);
    }
  }
  $('assets').innerHTML = rows.join('') || '<tr><td colspan="12" class="no-data">暂无资产</td></tr>';
}

function renderTrades(trades) {
  const rows = (trades || []).map((r) => `<tr>
    <td>${utcFull(r.exit_ts)}</td>
    <td><b>${escapeHtml(r.asset)}</b></td>
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
    $('mode').textContent = s.mode === 'live' ? '实盘' : '纸面';
    $('mode').className = s.mode === 'live' ? 'red' : 'green';
    $('cfgver').textContent = 'v' + (s.config_version ?? 0);
    $('uptime').textContent = '运行 ' + Math.round(s.uptime_s || 0) + ' 秒';
    $('entries').textContent = s.entries_allowed ? '允许' : '禁止';
    $('entries').className = 'stat-value ' + (s.entries_allowed ? 'green' : 'red');
    $('entries-sub').textContent = s.entries_allowed
      ? '可开新仓' : (s.entries_block_reason || '');
    $('pause').textContent = s.paused ? '恢复新开仓' : '暂停新开仓';
    const ss = s.same_side_open || {};
    const sideLimit = s.max_same_side_positions ?? '--';
    $('mode-note').textContent = (s.mode === 'live'
      ? '实盘模式：真实 CLOB FAK 订单'
      : '纸面模式：实时订单簿模拟成交，已扣 0.07 taker 费')
      + ` · 同向 UP ${ss.UP ?? 0}/${sideLimit} · DOWN ${ss.DOWN ?? 0}/${sideLimit}`;
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
