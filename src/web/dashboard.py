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

<div class="cards">
  <div class="card"><span>今日净 PnL (UTC)</span><strong id="today-pnl">--</strong>
    <div class="muted" id="today-sub">--</div></div>
  <div class="card"><span>累计净 PnL</span><strong id="total-pnl">--</strong>
    <div class="muted" id="total-sub">--</div></div>
  <div class="card"><span>胜率</span><strong id="win-rate">--</strong>
    <div class="muted" id="win-sub">--</div></div>
  <div class="card"><span>盈亏比</span><strong id="profit-factor">--</strong>
    <div class="muted" id="pf-sub">--</div></div>
  <div class="card"><span>累计手续费</span><strong id="fees">--</strong>
    <div class="muted">taker 0.07 · net 已扣</div></div>
  <div class="card"><span>新开仓</span><strong id="entries">--</strong>
    <div class="muted" id="entries-sub">--</div></div>
</div>

<div class="controls">
  <button id="pause">暂停新开仓</button>
  <button id="flatten" class="danger">全部市价平仓</button>
  <a href="/config"><button type="button">策略配置</button></a>
  <a href="/trades"><button type="button">全部交易</button></a>
  <span class="msg" id="msg"></span>
</div>

<h2>资产 / 当前窗口</h2>
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

<h2>最近交易 <a href="/trades" class="muted" style="text-transform:none;letter-spacing:0">全部 →</a></h2>
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
"""

_SCRIPT = r"""
let STATE = null;

function renderAssets(s) {
  const rows = [];
  for (const [asset, a] of Object.entries(s.assets || {})) {
    const cfg = a.config || {};
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
        : '-';
      rows.push(`<tr>
        <td><b>${escapeHtml(asset)}</b></td>
        <td>${enabledPill}</td>
        <td>${escapeHtml(w.slug || '-')}<br><span class="muted">${utcTime(w.start_ts)}</span></td>
        <td>${escapeHtml(side.phase || '-')}</td>
        <td>${escapeHtml(sideKey)}</td>
        <td>${escapeHtml(side.status || '-')}</td>
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

function renderTrades(payload) {
  const rows = (payload.trades || []).map((r) => `<tr>
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
  $('today-pnl').className = cls(t.net);
  $('today-sub').textContent = `${t.trades || 0} 笔 · ${t.wins || 0} 胜 / ${t.losses || 0} 负`;
  $('total-pnl').textContent = money(a.net, 4);
  $('total-pnl').className = cls(a.net);
  $('total-sub').textContent = `${a.trades || 0} 笔 · 毛 ${fmt(a.gross, 2)} / 费 ${fmt(a.fees, 2)}`;
  $('win-rate').textContent = pct(a.win_rate);
  $('win-rate').className = '';
  $('win-sub').textContent = `${a.wins || 0} 胜 / ${a.losses || 0} 负`;
  $('profit-factor').textContent = a.profit_factor === null
    ? (a.wins ? '∞' : '--') : fmt(a.profit_factor, 2);
  $('profit-factor').className = '';
  $('pf-sub').textContent = `均赢 ${money(a.avg_win, 2)} · 均亏 ${money(a.avg_loss, 2)}`;
  $('fees').textContent = fmt(a.fees, 2) + ' U';
  $('fees').className = 'neg';
}

async function refresh() {
  try {
    const s = await getJSON('/api/status');
    STATE = s;
    $('mode').textContent = s.mode === 'live' ? '实盘' : '纸面';
    $('mode').className = s.mode === 'live' ? 'neg' : 'pos';
    $('cfgver').textContent = 'v' + (s.config_version ?? 0);
    $('uptime').textContent = '运行 ' + Math.round(s.uptime_s || 0) + ' 秒';
    $('entries').textContent = s.entries_allowed ? '允许' : '禁止';
    $('entries').className = s.entries_allowed ? 'pos' : 'neg';
    $('entries-sub').textContent = s.entries_allowed
      ? '可开新仓' : (s.entries_block_reason || '');
    $('pause').textContent = s.paused ? '恢复新开仓' : '暂停新开仓';
    renderAssets(s);

    const [stats, trades] = await Promise.all([
      getJSON('/api/stats'),
      getJSON('/api/trades?limit=15'),
    ]);
    renderStats(stats);
    renderTrades(trades);

    $('msg').textContent = s.mode === 'paper'
      ? '纸面模式：按实时订单簿模拟成交，已扣 0.07 taker 费'
      : '实盘模式';
    $('msg').className = 'msg ' + (s.mode === 'live' ? 'red' : 'muted');
  } catch (e) {
    $('msg').textContent = String(e);
    $('msg').className = 'msg red';
  }
}

async function post(url, body) {
  const r = await fetch(url, {
    method: 'POST',
    headers: {'Content-Type': 'application/json'},
    body: JSON.stringify(body),
  });
  if (!r.ok) {
    $('msg').textContent = '请求失败: HTTP ' + r.status;
    $('msg').className = 'msg red';
    return null;
  }
  return await r.json();
}

$('pause').onclick = async () => {
  const next = !(STATE && STATE.paused);
  await post('/api/pause', {paused: next, reason: next ? 'dashboard' : ''});
  refresh();
};
$('flatten').onclick = async () => {
  const c = prompt('这会以市价卖出所有未平仓仓位。输入 FLATTEN 确认：');
  if (c !== 'FLATTEN') return;
  const r = await post('/api/flatten', {confirm: c});
  if (r) {
    $('msg').textContent = '已尝试平仓 ' + (r.sold ?? 0) + ' 笔';
    $('msg').className = 'msg orange';
  }
  refresh();
};

refresh();
setInterval(refresh, 2000);
"""

INDEX_HTML = render_page("控制台", "/", _BODY, _SCRIPT)
