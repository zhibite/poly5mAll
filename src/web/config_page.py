"""Per-asset runtime configuration page (``/config``)."""
from __future__ import annotations

from src.web.ui import render_page

_BODY = r"""
<div class="page-head">
  <div>
    <h1>策略配置</h1>
    <div class="page-sub">6 币 × 5m/15m（默认只开 6 个 5m 市场）· 15m 与 BTC 默认关闭 · 覆盖持久化到 data/runtime_config.json</div>
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
    <div class="stat-label">已启用市场</div>
    <div class="stat-value green" id="enabled-count">--</div>
    <div class="stat-sub">默认 6 个 5m 市场（15m / BTC 关闭）</div>
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

<div class="alert info">
  <b>交易模式：</b>每个市场可在下表单独选择<b>纸盘</b>或<b>实盘</b>。切换实盘会下真实
  订单：要求 .env 允许实盘（<span class="mono">PAPER_TRADING=false</span> 且
  <span class="mono">LIVE_ENABLED=true</span>）、凭证可用，并且<b>该市场无持仓、
  无在途入场</b>。已有仓位保持开仓时的模式；切换会持久化到运行时配置。
</div>

<div class="section">
  <h2>全局风控</h2>
  <div class="controls" style="margin:0">
    <span>当日亏损上限
      <input id="daily-loss" type="number" min="0" step="1"
             style="width:90px" value="40"> U
    </span>
    <span class="control-meta">按 UTC 日统计；当天已实现净亏损达到该值后
      停止新开仓（0=不限制），已有仓位照常管理。</span>
  </div>
  <div class="controls" style="margin:8px 0 0">
    <span>单窗同向上限
      <input id="window-side-limit" type="number" min="0" step="1"
             style="width:80px" value="2"> 笔
    </span>
    <span class="control-meta">同一市场窗口内（5m 与 15m 分别计数）同方向最多
      N 笔（含在途入场，0=不限），修改后立即影响下一次入场判断。</span>
  </div>
  <div class="controls" style="margin:8px 0 0">
    <span>日内回撤暂停
      <input id="drawdown-limit" type="number" min="0" step="1"
             style="width:90px" value="16"> U，暂停
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
    <span class="control-meta">同一市场窗口内双向、所有市场合计
      （5m 与 15m 分开计数，含在途入场，0=不限制）。</span>
  </div>
  <div class="controls" style="margin:8px 0 0">
    <span>全局同时持仓上限
      <input id="open-position-limit" type="number" min="0" step="1"
             style="width:80px" value="6"> 笔
    </span>
    <span>全局总敞口
      <input id="open-notional-limit" type="number" min="0" step="1"
             style="width:90px" value="10"> U
    </span>
    <span class="control-meta">跨 5m + 15m、双向、所有市场合计；含在途入场和
      等待结算的仓位（0=不限制）。这是全部市场的合计风险上限。</span>
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
  <b>生效时机：</b>启用/关闭与全局风控（日亏损、回撤、单窗/全局并发上限）
  <b>立即</b> 影响下一次入场判断；方向、entry、TP、止损、每笔仓位在
  <b>该市场下一个窗口</b> 生效。已有仓位始终按原参数止盈/止损，未触发的
  剩余份额在窗口结束后按 Gamma 0/1 结算。密钥、端口、`TP_EXECUTION_MODE` 和
  流动性过滤不在本页，需改 <span class="mono">.env</span> /
  <span class="mono">config.toml</span>；费率（官方费率，只读）见下表。
</div>

<div class="section">
  <h2>市场参数（7 币 × 5m/15m，共 14 个）</h2>
  <div class="table-wrap">
    <table>
      <thead><tr>
        <th>启用</th><th>币种 / 周期</th><th>模式</th><th>方向</th>
        <th class="num">费率</th>
        <th class="num">入场时间</th>
        <th class="num">入场价 ≤</th><th class="num">止盈 %</th>
        <th class="num">止损 %</th>
        <th class="num">每笔 U</th>
      </tr></thead>
      <tbody id="rows"><tr><td colspan="10" class="no-data">加载中...</td></tr></tbody>
    </table>
  </div>
</div>

<div class="save-bar">
  <button id="save" class="btn btn-primary">保存配置</button>
  <button id="reset" class="btn btn-ghost">恢复默认（6 币 5m，15m / BTC 关闭）</button>
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

function render() {
  const cfg = DATA;
  $('version').textContent = 'v' + (cfg.version ?? 0);
  $('runtime-path').textContent = cfg.runtime_path || '(not persisted)';
  const liveCount = (cfg.assets || []).filter((a) => a.mode === 'live').length;
  const paperCount = (cfg.assets || []).filter((a) => a.mode !== 'live').length;
  $('mode').textContent = `实盘 ${liveCount} · 纸盘 ${paperCount}`;
  $('mode').className = 'stat-value ' + (liveCount ? 'red' : 'green');
  $('fee-rate').textContent = fmt(Number(cfg.taker_fee_rate) * 100, 1) + '%';
  $('global-stake').textContent = fmt(cfg.stake_usd, 0) + ' U';
  $('same-limit').textContent = String(cfg.max_same_side_positions ?? '--')
    + '（单窗 ' + String(cfg.max_same_side_per_window ?? '--') + '）'
    + ' · 全局 ' + String(cfg.max_open_positions ?? '--') + '笔/'
    + fmt(cfg.max_open_notional_usd, 0) + 'U';
  $('min-depth').textContent = fmt(cfg.min_entry_bid_depth_usd, 0);
  const enabled = (cfg.assets || []).filter((a) => a.enabled).length;
  $('enabled-count').textContent = enabled + ' / ' + (cfg.assets || []).length;
  $('entries').textContent = cfg.paused ? '已暂停' : '允许';
  $('entries').className = 'stat-value ' + (cfg.paused ? 'red' : 'green');
  $('daily-loss').value = cfg.max_daily_loss_usd ?? 40;
  $('window-side-limit').value = cfg.max_same_side_per_window ?? 2;
  $('drawdown-limit').value = cfg.max_intraday_drawdown_usd ?? 16;
  $('drawdown-pause').value = cfg.intraday_drawdown_pause_min ?? 60;
  $('window-position-limit').value = cfg.max_positions_per_window ?? 3;
  $('window-notional-limit').value = cfg.max_window_notional_usd ?? 6;
  $('open-position-limit').value = cfg.max_open_positions ?? 6;
  $('open-notional-limit').value = cfg.max_open_notional_usd ?? 10;
  $('perf-lookback').value = cfg.asset_perf_lookback_days ?? 7;
  $('perf-min-pnl').value = cfg.asset_perf_min_pnl_usd ?? -3;
  $('perf-stake-mult').value = cfg.asset_perf_stake_mult ?? 0.5;
  $('perf-disable-pnl').value = cfg.asset_perf_disable_pnl_usd ?? -6;

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
    const entryEnd = Number(
      a.entry_window_end_s ?? (a.interval === '15m' ? 180 : 60)
    );
    const windowSec = Number(
      a.window_sec ?? (a.interval === '15m' ? 900 : 300)
    );
    const mode = a.mode === 'live' ? 'live' : 'paper';
    const canLive = !!(cfg.live_allowed && cfg.live_available);
    return `<tr data-market="${escapeHtml(a.market)}"
                data-asset="${escapeHtml(a.asset)}">
      <td><input type="checkbox" data-k="enabled" ${on ? 'checked' : ''}></td>
      <td><b>${escapeHtml(a.asset)}</b>
        <span class="pill blue">${escapeHtml(a.interval)}</span><br>
        <span class="pill ${on ? 'green' : ''}" data-role="state">${on ? '启用' : '关闭'}</span>
      </td>
      <td>
        <select data-k="mode" data-prev="${mode}" style="width:82px"
            title="${canLive ? '' : '实盘不可用：需要 .env 允许且凭证可用'}">
          <option value="paper" ${mode === 'paper' ? 'selected' : ''}>纸盘</option>
          <option value="live" ${mode === 'live' ? 'selected' : ''}
              ${canLive ? '' : 'disabled'}>实盘</option>
        </select>
      </td>
      <td>
        <select data-k="sides" style="width:118px">
          <option value="DOWN" ${sideValue(a) === 'DOWN' ? 'selected' : ''}>DOWN</option>
          <option value="UP" ${sideValue(a) === 'UP' ? 'selected' : ''}>UP</option>
          <option value="BOTH" ${sideValue(a) === 'BOTH' ? 'selected' : ''}>BOTH（双向）</option>
        </select>
      </td>
      <td class="num mono">${(Number(a.taker_fee_rate ?? cfg.taker_fee_rate) * 100).toFixed(1)}%</td>
      <td class="num mono" title="窗口总长 ${windowSec}s">0–${entryEnd}s</td>
      <td class="num"><input type="number" data-k="entry_thresh" step="0.01"
          min="0.01" max="0.99" style="width:90px" value="${a.entry_thresh}"></td>
      <td class="num"><input type="number" data-k="tp_pct" step="0.5"
          min="0.1" max="1000" style="width:90px"
          value="${(Number(a.tp_pct) * 100).toFixed(1)}"></td>
      <td class="num"><input type="number" data-k="stop_loss_pct" step="5"
          min="0" max="99" style="width:80px"
          value="${(Number(a.stop_loss_pct ?? 0) * 100).toFixed(1)}"></td>
      <td class="num"><input type="number" data-k="stake_usd" step="1"
          min="1" style="width:90px" value="${stake}"
          placeholder="${cfg.stake_usd}"></td>
    </tr>`;
  }).join('') || '<tr><td colspan="10" class="no-data">没有市场</td></tr>';

  for (const cb of document.querySelectorAll('#rows input[data-k="enabled"]')) {
    cb.addEventListener('change', () => {
      const state = cb.closest('tr').querySelector('[data-role="state"]');
      if (!state) return;
      state.textContent = cb.checked ? '启用' : '关闭';
      state.className = 'pill ' + (cb.checked ? 'green' : '');
    });
  }
  for (const sel of document.querySelectorAll('#rows select[data-k="mode"]')) {
    sel.addEventListener('change', async () => {
      const tr = sel.closest('tr');
      const market = tr.getAttribute('data-market');
      const prev = sel.dataset.prev || 'paper';
      const target = sel.value;
      if (target === prev) return;
      if (target === 'live') {
        const c = prompt(
          `将 ${market} 切换到实盘会下真实订单。\n` +
          `要求该市场无持仓、无在途入场。输入 LIVE 确认：`
        );
        if (c !== 'LIVE') { sel.value = prev; return; }
      }
      sel.disabled = true;
      try {
        await postJSON('/api/mode', {
          market,
          mode: target,
          confirm: target === 'live' ? 'LIVE' : '',
        });
        sel.dataset.prev = target;
        $('msg').textContent = `✅ ${market} 已切换到` +
          (target === 'live' ? '实盘' : '纸盘');
        $('msg').className = 'msg green';
      } catch (e) {
        sel.value = prev;
        $('msg').textContent = '⚠️ ' + e.message;
        $('msg').className = 'msg red';
      } finally {
        sel.disabled = false;
      }
    });
  }
}

function collect() {
  const assets = {};
  for (const tr of document.querySelectorAll('#rows tr[data-market]')) {
    const market = tr.getAttribute('data-market');
    const get = (k) => tr.querySelector(`[data-k="${k}"]`);
    const enabled = get('enabled').checked;
    const sides = get('sides').value;
    const entry = Number(get('entry_thresh').value);
    const tpPct = Number(get('tp_pct').value);
    const slPct = Number(get('stop_loss_pct').value);
    const stakeRaw = get('stake_usd').value;
    const stake = stakeRaw === '' ? null : Number(stakeRaw);

    if (!(entry > 0 && entry < 1)) {
      throw new Error(`${market}: entry 必须在 (0, 1) 之间`);
    }
    if (!(tpPct > 0)) throw new Error(`${market}: TP% 必须 > 0`);
    if (entry * (1 + tpPct / 100) >= 1) {
      throw new Error(`${market}: entry × (1 + TP%) 必须 < 1`);
    }
    if (!(slPct >= 0 && slPct < 100)) {
      throw new Error(`${market}: 止损% 必须在 [0, 100) 之间（0 = 关闭）`);
    }
    if (stake !== null && !(stake > 0)) {
      throw new Error(`${market}: 每笔 U 必须 > 0 或留空`);
    }
    assets[market] = {
      enabled,
      sides,
      entry_thresh: entry,
      tp_pct: tpPct / 100,
      stop_loss_pct: slPct / 100,
      stake_usd: stake,
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
  const openPositionLimit = Number($('open-position-limit').value);
  if (!(Number.isInteger(openPositionLimit) && openPositionLimit >= 0)) {
    throw new Error('全局同时持仓上限必须是 >= 0 的整数（0 = 不限制）');
  }
  const openNotionalLimit = Number($('open-notional-limit').value);
  if (!(openNotionalLimit >= 0)) {
    throw new Error('全局总敞口必须是 >= 0 的数值（0 = 不限制）');
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
    max_open_positions: openPositionLimit,
    max_open_notional_usd: openNotionalLimit,
    asset_perf_lookback_days: perfLookback,
    asset_perf_min_pnl_usd: perfMinPnl,
    asset_perf_stake_mult: perfStakeMult,
    asset_perf_disable_pnl_usd: perfDisablePnl,
  };
}

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
      '；参数从各市场下一个窗口开始生效';
    $('msg').className = 'msg green';
  } catch (e) {
    $('msg').textContent = '⚠️ ' + e.message;
    $('msg').className = 'msg red';
  } finally {
    $('save').disabled = false;
  }
}

async function reset() {
  if (!confirm('恢复为 config.toml 的默认值？\n默认只开其余 6 个币的 5m 市场，15m 与 BTC 关闭。')) return;
  $('msg').textContent = '恢复中...';
  try {
    const r = await fetch('/api/config/reset', {method: 'POST'});
    const data = await r.json().catch(() => ({}));
    if (!r.ok) throw new Error(data.detail || ('HTTP ' + r.status));
    DATA = data;
    render();
    $('msg').textContent = '✅ 已恢复默认（6 个 5m 市场，15m / BTC 关闭）';
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
