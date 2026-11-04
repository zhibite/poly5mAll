"""Trade history + profit statistics page (``/trades``)."""
from __future__ import annotations

from src.web.ui import render_page

_BODY = r"""
<div class="page-head">
  <div>
    <h1>交易记录</h1>
    <div class="page-sub">已平仓交易 · 净 PnL 已扣 0.07 taker 费 · 表格时间为浏览器本地时间</div>
  </div>
  <div class="head-meta">数据源 <span class="mono">data/trades.jsonl</span></div>
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
    <div class="stat-label">最大回撤</div>
    <div class="stat-value red" id="max-dd">--</div>
    <div class="stat-sub">按平仓顺序</div>
  </div>
</div>

<div class="control-bar">
  <div class="control-status">
    <span class="control-label">筛选</span>
    <select id="f-asset" style="width:140px">
      <option value="">全部币种</option>
    </select>
    <select id="f-reason" style="width:150px">
      <option value="">全部原因</option>
      <option value="tp">tp（止盈）</option>
      <option value="stop_loss">stop_loss（价格止损）</option>
      <option value="time_exit">time_exit（240s 强平）</option>
      <option value="settle">settle（结算）</option>
      <option value="shutdown">shutdown（退出）</option>
    </select>
    <select id="f-mode" style="width:120px">
      <option value="">全部模式</option>
      <option value="paper">纸面</option>
      <option value="live">实盘</option>
    </select>
  </div>
  <div class="controls" style="margin:0">
    <button id="refresh-btn" class="btn btn-ghost btn-sm">刷新</button>
    <span class="msg" id="msg"></span>
  </div>
</div>

<div class="section">
  <h2>交易明细 <span class="pill" id="trade-count">0</span></h2>
  <div class="table-wrap">
    <table>
      <thead><tr>
        <th>时间</th><th>资产</th><th>方向</th><th>原因</th><th>模式</th>
        <th class="num">入场</th>
        <th class="num">TP 目标</th><th class="num">观察价</th>
        <th class="num">实际卖价</th><th class="num">份额</th>
        <th class="num">毛 PnL</th><th class="num">费用</th><th class="num">净 PnL</th>
        <th class="num">持有</th>
      </tr></thead>
      <tbody id="rows"><tr><td colspan="14" class="no-data">加载中...</td></tr></tbody>
    </table>
  </div>
  <div class="pager">
    <button id="prev" class="btn btn-ghost btn-sm">上一页</button>
    <span class="pager-info" id="page-info">--</span>
    <button id="next" class="btn btn-ghost btn-sm">下一页</button>
  </div>
</div>

<div class="section">
  <h2>观察价止盈统计</h2>
  <div class="table-wrap">
    <table>
      <thead><tr><th>指标</th><th class="num">数值</th></tr></thead>
      <tbody id="tp-exec"><tr><td colspan="2" class="no-data">加载中...</td></tr></tbody>
    </table>
  </div>
  <div class="muted" style="font-size:11px;margin-top:8px;line-height:1.6">
    观察价 = TP 触发时的中间价；实际卖价 = 订单簿成交价。
    “观察价 &gt; 目标”表示触发时价格已越过 target（越价止盈）。
    旧记录没有观察价字段，会单独计入“缺失观察数据”。
  </div>
</div>

<div class="section">
  <h2>分币种统计</h2>
  <div class="table-wrap">
    <table>
      <thead><tr>
        <th>资产</th><th class="num">笔数</th><th class="num">胜/负</th>
        <th class="num">胜率</th><th class="num">净 PnL</th><th class="num">毛 PnL</th>
        <th class="num">费用</th><th class="num">最大回撤</th>
      </tr></thead>
      <tbody id="by-asset"><tr><td colspan="8" class="no-data">加载中...</td></tr></tbody>
    </table>
  </div>
</div>

<div class="section">
  <h2>按原因统计</h2>
  <div class="table-wrap">
    <table>
      <thead><tr>
        <th>原因</th><th class="num">笔数</th><th class="num">胜/负</th>
        <th class="num">净 PnL</th><th class="num">费用</th><th class="num">胜率</th>
      </tr></thead>
      <tbody id="by-reason"><tr><td colspan="6" class="no-data">加载中...</td></tr></tbody>
    </table>
  </div>
</div>

<div class="section">
  <h2>按日统计</h2>
  <div class="table-wrap">
    <table>
      <thead><tr>
        <th>日期 (UTC)</th><th class="num">笔数</th><th class="num">胜/负</th>
        <th class="num">净 PnL</th><th class="num">费用</th><th class="num">胜率</th>
      </tr></thead>
      <tbody id="by-day"><tr><td colspan="6" class="no-data">加载中...</td></tr></tbody>
    </table>
  </div>
</div>
"""

_SCRIPT = r"""
let OFFSET = 0;
const LIMIT = 25;
let TOTAL = 0;
let ASSET_OPTIONS_LOADED = false;

function reasonLabel(r) {
  return ({
    tp: 'tp（止盈）',
    stop_loss: 'stop_loss（价格止损）',
    time_exit: 'time_exit（240s 强平）',
    settle: 'settle（结算）',
    shutdown: 'shutdown（退出）',
  })[String(r || '').toLowerCase()] || (r || '--');
}

function renderStats(stats) {
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
  $('max-dd').textContent = fmt(a.max_drawdown, 2) + ' U';

  renderTpExec(stats.tp_execution || {});

  const byAsset = Object.entries(stats.by_asset || {});
  $('by-asset').innerHTML = byAsset.map(([asset, s]) => `<tr>
    <td><b>${escapeHtml(asset)}</b></td>
    <td class="num">${s.trades || 0}</td>
    <td class="num">${s.wins || 0} / ${s.losses || 0}</td>
    <td class="num">${pct(s.win_rate)}</td>
    <td class="num ${cls(s.net)}">${money(s.net, 4)}</td>
    <td class="num">${fmt(s.gross, 2)}</td>
    <td class="num red">${fmt(s.fees, 2)}</td>
    <td class="num red">${fmt(s.max_drawdown, 2)}</td>
  </tr>`).join('') || '<tr><td colspan="8" class="no-data">暂无数据</td></tr>';

  const byReason = Object.entries(stats.by_reason || {});
  $('by-reason').innerHTML = byReason.map(([reason, s]) => `<tr>
    <td>${escapeHtml(reasonLabel(reason))}</td>
    <td class="num">${s.trades || 0}</td>
    <td class="num">${s.wins || 0} / ${s.losses || 0}</td>
    <td class="num ${cls(s.net)}">${money(s.net, 4)}</td>
    <td class="num red">${fmt(s.fees, 2)}</td>
    <td class="num">${pct(s.win_rate)}</td>
  </tr>`).join('') || '<tr><td colspan="6" class="no-data">暂无数据</td></tr>';

  const byDay = Object.entries(stats.by_day || {}).sort((a, b) => b[0].localeCompare(a[0]));
  $('by-day').innerHTML = byDay.map(([day, s]) => `<tr>
    <td>${escapeHtml(day)}</td>
    <td class="num">${s.trades || 0}</td>
    <td class="num">${s.wins || 0} / ${s.losses || 0}</td>
    <td class="num ${cls(s.net)}">${money(s.net, 4)}</td>
    <td class="num red">${fmt(s.fees, 2)}</td>
    <td class="num">${pct(s.win_rate)}</td>
  </tr>`).join('') || '<tr><td colspan="6" class="no-data">暂无数据</td></tr>';
}

function renderTpExec(tp) {
  const n = tp.with_observation || 0;
  const rows = [
    ['TP 笔数', String(tp.tp_trades || 0)],
    ['有观察数据', `${n}${tp.missing_observation
      ? ` · 旧记录缺失 ${tp.missing_observation}` : ''}`],
    ['观察价 > 目标（越价止盈）',
      `${tp.observed_above_target || 0} / ${n} · ${pct(tp.observed_above_rate)}`],
    ['实际成交 > 目标',
      `${tp.actual_above_target || 0} / ${n} · ${pct(tp.actual_above_rate)}`],
    ['平均观察越价',
      `${fmt(tp.avg_observed_edge, 4)} /份 · ${fmt(tp.avg_observed_edge_bps, 1)} bps`],
    ['平均实际越价',
      `${fmt(tp.avg_actual_edge, 4)} /份 · ${fmt(tp.avg_actual_edge_bps, 1)} bps`],
    ['实际成交 vs 观察价',
      `${fmt(tp.avg_fill_vs_observed, 4)} /份 · ${fmt(tp.avg_fill_vs_observed_bps, 1)} bps`],
  ];
  $('tp-exec').innerHTML = rows.map(([k, v]) => `<tr>
    <td>${escapeHtml(k)}</td><td class="num">${escapeHtml(v)}</td>
  </tr>`).join('') || '<tr><td colspan="2" class="no-data">暂无数据</td></tr>';
}

function renderTrades(payload) {
  const rows = payload.trades || [];
  TOTAL = payload.total || 0;
  $('trade-count').textContent = TOTAL;
  if (!rows.length) {
    $('rows').innerHTML = '<tr><td colspan="14" class="no-data">暂无交易</td></tr>';
  } else {
    $('rows').innerHTML = rows.map((r) => {
      const fees = Number(r.entry_fee || 0) + Number(r.exit_fee || 0);
      const hold = Math.max(0, Number(r.exit_ts || 0) - Number(r.entry_ts || 0));
      const isTp = String(r.reason || '').toLowerCase() === 'tp';
      const target = isTp ? fmt(r.tp_price, 3) : '--';
      const observed = isTp && Number(r.tp_observed_price) > 0
        ? fmt(r.tp_observed_price, 3) : '--';
      return `<tr>
        <td>${utcFull(r.exit_ts)}</td>
        <td><b>${escapeHtml(r.asset)}</b></td>
        <td>${escapeHtml(r.side)}</td>
        <td>${escapeHtml(reasonLabel(r.reason))}</td>
        <td><span class="pill ${r.mode === 'live' ? 'red' : 'blue'}">${r.mode === 'live' ? '实盘' : '纸面'}</span></td>
        <td class="num">${fmt(r.entry_price, 3)}</td>
        <td class="num">${target}</td>
        <td class="num">${observed}</td>
        <td class="num">${fmt(r.exit_price, 3)}</td>
        <td class="num">${fmt(r.shares, 2)}</td>
        <td class="num ${cls(r.pnl_gross)}">${money(r.pnl_gross, 4)}</td>
        <td class="num red">${fmt(fees, 4)}</td>
        <td class="num ${cls(r.pnl_net)}">${money(r.pnl_net, 4)}</td>
        <td class="num">${fmt(hold, 0)}s</td>
      </tr>`;
    }).join('');
  }
  const pages = Math.max(1, Math.ceil(TOTAL / LIMIT));
  const page = Math.floor(OFFSET / LIMIT) + 1;
  $('page-info').textContent = `${page} / ${pages} 页 · 共 ${TOTAL} 笔`;
  $('prev').disabled = OFFSET <= 0;
  $('next').disabled = OFFSET + LIMIT >= TOTAL;
}

async function loadAssets() {
  if (ASSET_OPTIONS_LOADED) return;
  try {
    const cfg = await getJSON('/api/config');
    const sel = $('f-asset');
    for (const a of cfg.assets || []) {
      if ([...sel.options].some((o) => o.value === a.asset)) continue;
      const opt = document.createElement('option');
      opt.value = a.asset;
      opt.textContent = a.asset;
      sel.appendChild(opt);
    }
    ASSET_OPTIONS_LOADED = true;
  } catch (e) { /* filters are optional */ }
}

function filters() {
  const p = new URLSearchParams({
    limit: String(LIMIT),
    offset: String(OFFSET),
  });
  const asset = $('f-asset').value;
  const reason = $('f-reason').value;
  const mode = $('f-mode').value;
  if (asset) p.set('asset', asset);
  if (reason) p.set('reason', reason);
  if (mode) p.set('mode', mode);
  return p.toString();
}

async function load() {
  try {
    const [stats, trades] = await Promise.all([
      getJSON('/api/stats'),
      getJSON('/api/trades?' + filters()),
    ]);
    renderStats(stats);
    renderTrades(trades);
    $('msg').textContent = '自动刷新中';
    $('msg').className = 'msg muted';
  } catch (e) {
    $('msg').textContent = String(e);
    $('msg').className = 'msg red';
  }
}

function resetAndLoad() { OFFSET = 0; load(); }

$('f-asset').onchange = resetAndLoad;
$('f-reason').onchange = resetAndLoad;
$('f-mode').onchange = resetAndLoad;
$('prev').onclick = () => { OFFSET = Math.max(0, OFFSET - LIMIT); load(); };
$('next').onclick = () => {
  if (OFFSET + LIMIT < TOTAL) { OFFSET += LIMIT; load(); }
};
$('refresh-btn').onclick = resetAndLoad;

(async () => {
  await loadAssets();
  await load();
  setInterval(load, 5000);
})();
"""

TRADES_HTML = render_page("交易记录", "/trades", _BODY, _SCRIPT)
