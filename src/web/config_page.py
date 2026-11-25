"""Per-asset runtime configuration page (``/config``)."""
from __future__ import annotations

from src.web.ui import render_page

_BODY = r"""
<div class="page-head">
  <div>
    <h1>策略配置</h1>
    <div class="page-sub">7 个币的 5m 参数 · 默认关闭 BTC、启用其余 6 个币 · 覆盖持久化到 data/runtime_config.json</div>
  </div>
  <div class="head-meta">
    配置版本 <b id="version">--</b><br>
    <span class="mono" id="runtime-path">--</span><br>
    同向上限 <b id="same-limit">--</b> · 买盘深度 ≥
    <b id="min-depth">--</b> U
  </div>
</div>

<div class="stats-row">
  <div class="stat-card">
    <div class="stat-label">模式</div>
    <div class="stat-value" id="mode">--</div>
    <div class="stat-sub">paper / live</div>
  </div>
  <div class="stat-card">
    <div class="stat-label">默认 taker 费率</div>
    <div class="stat-value purple" id="fee-rate">--</div>
    <div class="stat-sub">fee = shares × rate × p × (1-p)；各币见下表</div>
  </div>
  <div class="stat-card">
    <div class="stat-label">已启用</div>
    <div class="stat-value green" id="enabled-count">--</div>
    <div class="stat-sub">默认 6 个币（BTC 关闭）</div>
  </div>
  <div class="stat-card">
    <div class="stat-label">全局每笔</div>
    <div class="stat-value" id="global-stake">--</div>
    <div class="stat-sub">未单独覆盖的币使用此值</div>
  </div>
  <div class="stat-card">
    <div class="stat-label">新开仓</div>
    <div class="stat-value" id="entries">--</div>
    <div class="stat-sub">暂停/风控状态</div>
  </div>
</div>

<div id="auth-alert"></div>

<div class="section">
  <h2>交易模式</h2>
  <div class="controls" style="margin:0">
    <span>当前：<b id="trade-mode">--</b></span>
    <span class="control-meta" id="mode-note"></span>
    <button id="mode-switch" class="btn btn-danger">切换到实盘</button>
  </div>
  <div class="alert info" style="margin:12px 0 0">
    切换实盘会下真实订单：要求 .env 允许实盘（<span class="mono">PAPER_TRADING=false</span>
    且 <span class="mono">LIVE_ENABLED=true</span>）、凭证可用，并且当前
    <b>无持仓、无在途入场</b>。切换会持久化到运行时配置；纸盘模式不会下真实订单。
  </div>
</div>

<div class="section">
  <h2>全局风控</h2>
  <div class="controls" style="margin:0">
    <span>当日亏损上限
      <input id="daily-loss" type="number" min="0" step="1"
             style="width:90px" value="20"> U
    </span>
    <span class="control-meta">按 UTC 日统计；当天已实现净亏损达到该值后
      停止新开仓（0=不限制），已有仓位照常管理。</span>
  </div>
  <div class="controls" style="margin:8px 0 0">
    <span>单窗同向上限
      <input id="window-side-limit" type="number" min="0" step="1"
             style="width:80px" value="2"> 笔
    </span>
    <span class="control-meta">同一 5m 窗口内同方向最多 N 笔
      （含在途入场，0=不限），从下一个 5m 窗口生效。</span>
  </div>
  <div class="controls" style="margin:8px 0 0">
    <span>日内回撤暂停
      <input id="drawdown-limit" type="number" min="0" step="1"
             style="width:90px" value="8"> U，暂停
      <input id="drawdown-pause" type="number" min="0" step="5"
             style="width:80px" value="60"> 分钟
    </span>
    <span class="control-meta">UTC 当日已实现 PnL 从日内高点回落达到该值后
      停止新开仓（0=不限）；暂停到期后自动恢复，并以恢复时的 PnL 重置
      回撤基准，已有仓位照常管理。</span>
  </div>
  <div class="controls" style="margin:8px 0 0">
    <span>单窗总笔数
      <input id="window-position-limit" type="number" min="0" step="1"
             style="width:80px" value="3"> 笔
    </span>
    <span>单窗总敞口
      <input id="window-notional-limit" type="number" min="0" step="1"
             style="width:90px" value="6"> U
    </span>
    <span class="control-meta">同一 5m 窗口内双向、所有币合计
      （含在途入场，0=不限制）。</span>
  </div>
  <div class="controls" style="margin:8px 0 0">
    <span>按币滚动表现降权</span>
    <span class="control-meta">回看
      <input id="perf-lookback" type="number" min="0" step="1"
             style="width:70px" value="7"> 天，已实现净 PnL ≤
      <input id="perf-min-pnl" type="number" step="1"
             style="width:80px" value="-3"> U 时仓位 ×
      <input id="perf-stake-mult" type="number" min="0" max="1" step="0.1"
             style="width:70px" value="0.5">，≤
      <input id="perf-disable-pnl" type="number" step="1"
             style="width:80px" value="-6"> U 时停止开仓
      （回看 0 天 = 关闭）。</span>
  </div>
</div>

<div class="alert info">
  <b>生效时机：</b>启用/关闭 <b>立即</b> 阻止/允许新开仓；方向、entry、TP、每笔仓位、
  强平时间在 <b>下一个 5m 窗口</b> 生效。已有仓位始终按原参数止盈/强平。
  密钥、端口、`TP_EXECUTION_MODE` 和流动性过滤不在本页，
  需改 <span class="mono">.env</span> / <span class="mono">config.toml</span>；
  费率（官方费率，只读）见下表。
</div>

<div class="section">
  <h2>5 分钟市场时间轴 <span class="pill blue" id="tp-mode-pill">observed</span></h2>
  <div class="timeline-bar" id="timeline-bar"></div>
  <div class="timing-grid" id="timing-grid">
    <div class="no-data">加载中...</div>
  </div>
  <div class="alert info" style="margin:14px 0 0">
    止盈公式：<span class="mono">target = 实际成交价 × (1 + TP%)</span>。
    默认 <span class="mono">observed</span>：触发后 FAK 卖出，成交价取当时最优
    bid。价格止损已移除，只保留时间强平；时间轴为全局默认，
    <b>下表每个币的强平开始/截止可单独覆盖</b>。
  </div>
</div>

<div class="section">
  <h2>7 个币的参数</h2>
  <div class="table-wrap">
    <table>
      <thead><tr>
        <th>启用</th><th>币种</th><th>方向</th>
        <th class="num">费率</th>
        <th class="num">entry ≤</th><th class="num">TP %</th>
        <th class="num">每笔 U</th>
        <th class="num">强平 s</th><th class="num">截止 s</th>
      </tr></thead>
      <tbody id="rows"><tr><td colspan="9" class="no-data">加载中...</td></tr></tbody>
    </table>
  </div>
</div>

<div class="save-bar">
  <button id="save" class="btn btn-primary">保存配置</button>
  <button id="reset" class="btn btn-ghost">恢复默认（6 币，BTC 关闭）</button>
  <span class="msg" id="msg"></span>
</div>

"""

_SCRIPT = r"""
let DATA = null;

function sideValue(a) {
  const sides = (a.sides || []).map((s) => String(s).toUpperCase());
  if (sides.includes('UP') && sides.includes('DOWN')) return 'BOTH';
  return sides[0] || 'DOWN';
}

function renderMode(cfg) {
  const live = cfg.mode === 'live';
  $('trade-mode').textContent = live ? '实盘' : '纸盘';
  $('trade-mode').className = live ? 'red' : 'green';
  const btn = $('mode-switch');
  const allowed = !!cfg.live_allowed;
  const avail = !!cfg.live_available;
  if (live) {
    btn.textContent = '切换回纸盘';
    btn.className = 'btn btn-ghost';
    btn.disabled = false;
    $('mode-note').textContent = '真实 CLOB 订单';
  } else if (!allowed) {
    btn.textContent = '实盘不可用（.env 未允许）';
    btn.className = 'btn btn-ghost';
    btn.disabled = true;
    $('mode-note').textContent = '需要 PAPER_TRADING=false 且 LIVE_ENABLED=true';
  } else if (!avail) {
    btn.textContent = '实盘不可用（凭证未配置）';
    btn.className = 'btn btn-ghost';
    btn.disabled = true;
    $('mode-note').textContent = 'CLOB 凭证缺失或初始化失败';
  } else {
    btn.textContent = '切换到实盘';
    btn.className = 'btn btn-danger';
    btn.disabled = false;
    $('mode-note').textContent = '切换前必须无持仓、无在途入场';
  }
}

function renderTiming(cfg) {
  const t = cfg.timing || {};
  const total = Number(t.window_sec || 300);
  const entry = Number(t.entry_window_end_s || 60);
  const tpStart = Number(t.tp_window_start_s || 60);
  const force = Number(t.force_exit_s || 240);
  const deadline = Number(t.force_exit_deadline_s || 295);
  const mode = cfg.tp_execution_mode || 'observed';

  const segs = [
    {label: '入场', dur: entry, cls: 'seg-entry',
     title: `0–${entry}s：观察，首次价格 ≤ entry 买入`},
    {label: '止盈', dur: Math.max(1, force - tpStart), cls: 'seg-tp',
     title: `${tpStart}–${force}s：价格 ≥ 成交价×(1+TP%) 卖出`},
    {label: '强平', dur: Math.max(1, deadline - force), cls: 'seg-sl',
     title: `${force}–${deadline}s：未止盈则 FAK 市价卖出，每秒重试`},
    {label: '结算', dur: Math.max(1, total - deadline), cls: 'seg-settle',
     title: `${deadline}–${total}s：未卖出则按 Gamma 0/1 结算`},
  ];
  $('timeline-bar').innerHTML = segs.map((s) => `<div
      class="timeline-seg ${s.cls}" style="flex:${s.dur}"
      title="${escapeHtml(s.title)}">${escapeHtml(s.label)}</div>`).join('');
  $('tp-mode-pill').textContent = mode;

  $('timing-grid').innerHTML = `
    <div class="timing-card">
      <div class="t-label">入场观察</div>
      <div class="t-time">0–${entry}s</div>
      <div class="t-desc">5 分钟市场开始后观察；该方向价格首次
        ≤ entry 时买入。每方向每窗口最多一笔。</div>
    </div>
    <div class="timing-card tp">
      <div class="t-label">止盈窗口</div>
      <div class="t-time">${tpStart}–${force}s</div>
      <div class="t-desc">价格 ≥ 实际成交价 × (1 + TP%) 时卖出；
        默认 observed，按当时最优 bid 成交。</div>
    </div>
    <div class="timing-card sl">
      <div class="t-label">时间止损 / 强平</div>
      <div class="t-time">${force}–${deadline}s</div>
      <div class="t-desc">到该币的强平时间仍未止盈，FAK 市价卖出并每秒重试；
        下表可按币覆盖开始/截止秒数。</div>
    </div>
    <div class="timing-card settle">
      <div class="t-label">结算</div>
      <div class="t-time">${deadline}–${total}s</div>
      <div class="t-desc">${deadline}s 仍无法卖出 → SETTLE_PENDING，
        窗口结束后按 Gamma 0/1 结算价记账。</div>
    </div>`;
}

function render() {
  const cfg = DATA;
  $('version').textContent = 'v' + (cfg.version ?? 0);
  $('runtime-path').textContent = cfg.runtime_path || '(not persisted)';
  $('mode').textContent = cfg.mode === 'live' ? '实盘' : '纸面';
  $('mode').className = 'stat-value ' + (cfg.mode === 'live' ? 'red' : 'green');
  $('fee-rate').textContent = fmt(Number(cfg.taker_fee_rate) * 100, 1) + '%';
  $('global-stake').textContent = fmt(cfg.stake_usd, 0) + ' U';
  $('same-limit').textContent = String(cfg.max_same_side_positions ?? '--')
    + '（单窗 ' + String(cfg.max_same_side_per_window ?? '--') + '）';
  $('min-depth').textContent = fmt(cfg.min_entry_bid_depth_usd, 0);
  const enabled = (cfg.assets || []).filter((a) => a.enabled).length;
  $('enabled-count').textContent = enabled + ' / ' + (cfg.assets || []).length;
  $('entries').textContent = cfg.paused ? '已暂停' : '允许';
  $('entries').className = 'stat-value ' + (cfg.paused ? 'red' : 'green');
  $('daily-loss').value = cfg.max_daily_loss_usd ?? 20;
  $('window-side-limit').value = cfg.max_same_side_per_window ?? 2;
  $('drawdown-limit').value = cfg.max_intraday_drawdown_usd ?? 8;
  $('drawdown-pause').value = cfg.intraday_drawdown_pause_min ?? 60;
  $('window-position-limit').value = cfg.max_positions_per_window ?? 3;
  $('window-notional-limit').value = cfg.max_window_notional_usd ?? 6;
  $('perf-lookback').value = cfg.asset_perf_lookback_days ?? 7;
  $('perf-min-pnl').value = cfg.asset_perf_min_pnl_usd ?? -3;
  $('perf-stake-mult').value = cfg.asset_perf_stake_mult ?? 0.5;
  $('perf-disable-pnl').value = cfg.asset_perf_disable_pnl_usd ?? -6;
  renderMode(cfg);
  renderTiming(cfg);

  if (cfg.web && !cfg.web.auth) {
    $('auth-alert').innerHTML =
      '<div class="alert err"><b>⚠️ 后端未设置 WEB_PASSWORD。</b>' +
      '当前 /config 和 /api/config 可被同网络任何人访问。请在 .env 设置 ' +
      '<span class="mono">WEB_PASSWORD</span> 后重新部署。</div>';
  } else {
    $('auth-alert').innerHTML = '';
  }

  $('rows').innerHTML = (cfg.assets || []).map((a) => {
    const stake = a.stake_usd === null || a.stake_usd === undefined
      ? '' : a.stake_usd;
    const on = !!a.enabled;
    return `<tr data-asset="${escapeHtml(a.asset)}">
      <td><input type="checkbox" data-k="enabled" ${on ? 'checked' : ''}></td>
      <td><b>${escapeHtml(a.asset)}</b><br>
        <span class="pill ${on ? 'green' : ''}" data-role="state">${on ? '启用' : '关闭'}</span>
      </td>
      <td>
        <select data-k="sides" style="width:118px">
          <option value="DOWN" ${sideValue(a) === 'DOWN' ? 'selected' : ''}>DOWN</option>
          <option value="UP" ${sideValue(a) === 'UP' ? 'selected' : ''}>UP</option>
          <option value="BOTH" ${sideValue(a) === 'BOTH' ? 'selected' : ''}>BOTH（双向）</option>
        </select>
      </td>
      <td class="num mono">${(Number(a.taker_fee_rate ?? cfg.taker_fee_rate) * 100).toFixed(1)}%</td>
      <td class="num"><input type="number" data-k="entry_thresh" step="0.01"
          min="0.01" max="0.99" style="width:90px" value="${a.entry_thresh}"></td>
      <td class="num"><input type="number" data-k="tp_pct" step="0.5"
          min="0.1" max="1000" style="width:90px"
          value="${(Number(a.tp_pct) * 100).toFixed(1)}"></td>
      <td class="num"><input type="number" data-k="stake_usd" step="1"
          min="1" style="width:90px" value="${stake}"
          placeholder="${cfg.stake_usd}"></td>
      <td class="num"><input type="number" data-k="force_exit_s" step="1"
          min="61" max="299" style="width:78px"
          value="${a.force_exit_s ?? 240}"></td>
      <td class="num"><input type="number" data-k="force_exit_deadline_s"
          step="1" min="61" max="300" style="width:78px"
          value="${a.force_exit_deadline_s ?? 295}"></td>
    </tr>`;
  }).join('') || '<tr><td colspan="9" class="no-data">没有资产</td></tr>';

  for (const cb of document.querySelectorAll('#rows input[data-k="enabled"]')) {
    cb.addEventListener('change', () => {
      const state = cb.closest('tr').querySelector('[data-role="state"]');
      if (!state) return;
      state.textContent = cb.checked ? '启用' : '关闭';
      state.className = 'pill ' + (cb.checked ? 'green' : '');
    });
  }
  // When the forced-exit time changes, pull the deadline along unless the
  // operator already set a later one.
  for (const inp of document.querySelectorAll('#rows input[data-k="force_exit_s"]')) {
    inp.addEventListener('change', () => {
      const tr = inp.closest('tr');
      const dl = tr.querySelector('[data-k="force_exit_deadline_s"]');
      const force = Number(inp.value);
      if (dl && (!dl.value || Number(dl.value) <= force)) {
        dl.value = String(Math.min(300, force + 15));
      }
    });
  }
}

function collect() {
  const assets = {};
  for (const tr of document.querySelectorAll('#rows tr[data-asset]')) {
    const asset = tr.getAttribute('data-asset');
    const get = (k) => tr.querySelector(`[data-k="${k}"]`);
    const enabled = get('enabled').checked;
    const sides = get('sides').value;
    const entry = Number(get('entry_thresh').value);
    const tpPct = Number(get('tp_pct').value);
    const stakeRaw = get('stake_usd').value;
    const stake = stakeRaw === '' ? null : Number(stakeRaw);
    const forceExit = Number(get('force_exit_s').value);
    const forceDeadline = Number(get('force_exit_deadline_s').value);

    if (!(entry > 0 && entry < 1)) {
      throw new Error(`${asset}: entry 必须在 (0, 1) 之间`);
    }
    if (!(tpPct > 0)) throw new Error(`${asset}: TP% 必须 > 0`);
    if (entry * (1 + tpPct / 100) >= 1) {
      throw new Error(`${asset}: entry × (1 + TP%) 必须 < 1`);
    }
    if (stake !== null && !(stake > 0)) {
      throw new Error(`${asset}: 每笔 U 必须 > 0 或留空`);
    }
    if (!(forceExit > 60 && forceExit < 300)) {
      throw new Error(`${asset}: 强平时间必须在 (60, 300) 秒之间`);
    }
    if (!(forceDeadline >= forceExit && forceDeadline <= 300)) {
      throw new Error(`${asset}: 强平截止必须 >= 强平开始且 <= 300 秒`);
    }
    assets[asset] = {
      enabled,
      sides,
      entry_thresh: entry,
      tp_pct: tpPct / 100,
      stop_loss_pct: 0.0,
      stake_usd: stake,
      force_exit_s: forceExit,
      force_exit_deadline_s: forceDeadline,
    };
  }
  const dailyLoss = Number($('daily-loss').value);
  if (!(dailyLoss >= 0)) {
    throw new Error('当日亏损上限必须 >= 0（0 = 不限制）');
  }
  const windowSideLimit = Number($('window-side-limit').value);
  if (!(Number.isInteger(windowSideLimit) && windowSideLimit >= 0)) {
    throw new Error('单窗同向上限必须是 >= 0 的整数（0 = 不限制）');
  }
  const drawdownLimit = Number($('drawdown-limit').value);
  if (!(drawdownLimit >= 0)) {
    throw new Error('日内回撤阈值必须是 >= 0 的数值（0 = 不限制）');
  }
  const drawdownPause = Number($('drawdown-pause').value);
  if (!(drawdownPause >= 0)) {
    throw new Error('回撤暂停时长必须是 >= 0 的分钟数（0 = 关闭）');
  }
  const windowPositionLimit = Number($('window-position-limit').value);
  if (!(Number.isInteger(windowPositionLimit) && windowPositionLimit >= 0)) {
    throw new Error('单窗总笔数必须是 >= 0 的整数（0 = 不限制）');
  }
  const windowNotionalLimit = Number($('window-notional-limit').value);
  if (!(windowNotionalLimit >= 0)) {
    throw new Error('单窗总敞口必须是 >= 0 的数值（0 = 不限制）');
  }
  const perfLookback = Number($('perf-lookback').value);
  if (!(perfLookback >= 0)) {
    throw new Error('表现回看天数必须 >= 0（0 = 关闭该规则）');
  }
  const perfMinPnl = Number($('perf-min-pnl').value);
  if (!Number.isFinite(perfMinPnl)) {
    throw new Error('降权阈值必须是数值');
  }
  const perfStakeMult = Number($('perf-stake-mult').value);
  if (!(perfStakeMult >= 0 && perfStakeMult <= 1)) {
    throw new Error('降权系数必须在 [0, 1] 之间');
  }
  const perfDisablePnl = Number($('perf-disable-pnl').value);
  if (!Number.isFinite(perfDisablePnl)) {
    throw new Error('停止阈值必须是数值');
  }
  return {
    assets,
    max_daily_loss_usd: dailyLoss,
    max_same_side_per_window: windowSideLimit,
    max_intraday_drawdown_usd: drawdownLimit,
    intraday_drawdown_pause_min: drawdownPause,
    max_positions_per_window: windowPositionLimit,
    max_window_notional_usd: windowNotionalLimit,
    asset_perf_lookback_days: perfLookback,
    asset_perf_min_pnl_usd: perfMinPnl,
    asset_perf_stake_mult: perfStakeMult,
    asset_perf_disable_pnl_usd: perfDisablePnl,
  };
}

$('mode-switch').onclick = async () => {
  const cur = DATA && DATA.mode;
  let body;
  if (cur === 'live') {
    if (!confirm('切换回纸盘模式？要求当前无持仓、无在途入场。')) return;
    body = {mode: 'paper', confirm: 'PAPER'};
  } else {
    const c = prompt('切换到实盘会下真实订单。输入 LIVE 确认：');
    if (c !== 'LIVE') return;
    body = {mode: 'live', confirm: 'LIVE'};
  }
  try {
    await postJSON('/api/mode', body);
    await load();
    $('msg').textContent = '✅ 已切换到' + (body.mode === 'live' ? '实盘' : '纸盘');
    $('msg').className = 'msg green';
  } catch (e) {
    $('msg').textContent = '⚠️ ' + e.message;
    $('msg').className = 'msg red';
  }
};

async function load() {
  try {
    DATA = await getJSON('/api/config');
    render();
    $('msg').textContent = '配置已加载';
    $('msg').className = 'msg muted';
  } catch (e) {
    $('msg').textContent = String(e);
    $('msg').className = 'msg red';
  }
}

async function save() {
  let body;
  try {
    body = collect();
  } catch (e) {
    $('msg').textContent = '⚠️ ' + e.message;
    $('msg').className = 'msg red';
    return;
  }
  $('save').disabled = true;
  $('msg').textContent = '保存中...';
  $('msg').className = 'msg muted';
  try {
    const r = await fetch('/api/config', {
      method: 'PUT',
      headers: {'Content-Type': 'application/json'},
      body: JSON.stringify(body),
    });
    const data = await r.json().catch(() => ({}));
    if (!r.ok) throw new Error(data.detail || ('HTTP ' + r.status));
    DATA = data;
    render();
    $('msg').textContent = '✅ 已保存，配置版本 v' + (DATA.version ?? 0) +
      '；其他参数从下一个 5m 窗口开始生效';
    $('msg').className = 'msg green';
  } catch (e) {
    $('msg').textContent = '⚠️ ' + e.message;
    $('msg').className = 'msg red';
  } finally {
    $('save').disabled = false;
  }
}

async function reset() {
  if (!confirm('恢复为 config.toml 的默认值？\n默认关闭 BTC、启用其余 6 个币。')) return;
  $('msg').textContent = '恢复中...';
  try {
    const r = await fetch('/api/config/reset', {method: 'POST'});
    const data = await r.json().catch(() => ({}));
    if (!r.ok) throw new Error(data.detail || ('HTTP ' + r.status));
    DATA = data;
    render();
    $('msg').textContent = '✅ 已恢复默认（6 币，BTC 关闭）';
    $('msg').className = 'msg green';
  } catch (e) {
    $('msg').textContent = '⚠️ ' + e.message;
    $('msg').className = 'msg red';
  }
}

$('save').onclick = save;
$('reset').onclick = reset;
load();
setInterval(async () => {
  try {
    const cfg = await getJSON('/api/config');
    $('version').textContent = 'v' + (cfg.version ?? 0);
    $('entries').textContent = cfg.paused ? '已暂停' : '允许';
    $('entries').className = 'stat-value ' + (cfg.paused ? 'red' : 'green');
  } catch (e) { /* keep the page usable */ }
}, 5000);
"""

CONFIG_HTML = render_page("策略配置", "/config", _BODY, _SCRIPT)
