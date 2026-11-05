"""Per-asset runtime configuration page (``/config``)."""
from __future__ import annotations

from src.web.ui import render_page

_BODY = r"""
<div class="page-head">
  <div>
    <h1>策略配置</h1>
    <div class="page-sub">7 个币的 5m 参数 · 默认只启用 BTC · 覆盖持久化到 data/runtime_config.json</div>
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
    <div class="stat-label">taker 费率</div>
    <div class="stat-value purple" id="fee-rate">--</div>
    <div class="stat-sub">fee = shares × rate × p × (1-p)</div>
  </div>
  <div class="stat-card">
    <div class="stat-label">已启用</div>
    <div class="stat-value green" id="enabled-count">--</div>
    <div class="stat-sub">默认仅 BTC</div>
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
  <b>生效时机：</b>启用/关闭 <b>立即</b> 阻止/允许新开仓；方向、entry、TP、每笔仓位在
  <b>下一个 5m 窗口</b> 生效。已有仓位始终按原参数止盈/240s 强平。
  密钥、端口、费率、`TP_EXECUTION_MODE`、流动性过滤和同向上限不在本页，
  需改 <span class="mono">.env</span> / <span class="mono">config.toml</span>。
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
    bid。价格止损已移除，只保留 240s 时间强平。
  </div>
</div>

<div class="section">
  <h2>7 个币的参数</h2>
  <div class="table-wrap">
    <table>
      <thead><tr>
        <th>启用</th><th>币种</th><th>方向</th>
        <th class="num">entry ≤</th><th class="num">TP %</th>
        <th class="num">每笔 U</th>
      </tr></thead>
      <tbody id="rows"><tr><td colspan="6" class="no-data">加载中...</td></tr></tbody>
    </table>
  </div>
</div>

<div class="save-bar">
  <button id="save" class="btn btn-primary">保存配置</button>
  <button id="reset" class="btn btn-ghost">恢复默认（仅 BTC）</button>
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
      <div class="t-desc">到 ${force}s 仍未止盈，FAK 市价卖出并每秒重试；
        这是时间强平。</div>
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
  $('fee-rate').textContent = fmt(cfg.taker_fee_rate, 4);
  $('global-stake').textContent = fmt(cfg.stake_usd, 0) + ' U';
  $('same-limit').textContent = String(cfg.max_same_side_positions ?? '--');
  $('min-depth').textContent = fmt(cfg.min_entry_bid_depth_usd, 0);
  const enabled = (cfg.assets || []).filter((a) => a.enabled).length;
  $('enabled-count').textContent = enabled + ' / ' + (cfg.assets || []).length;
  $('entries').textContent = cfg.paused ? '已暂停' : '允许';
  $('entries').className = 'stat-value ' + (cfg.paused ? 'red' : 'green');
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
      <td class="num"><input type="number" data-k="entry_thresh" step="0.01"
          min="0.01" max="0.99" style="width:90px" value="${a.entry_thresh}"></td>
      <td class="num"><input type="number" data-k="tp_pct" step="0.5"
          min="0.1" max="1000" style="width:90px"
          value="${(Number(a.tp_pct) * 100).toFixed(1)}"></td>
      <td class="num"><input type="number" data-k="stake_usd" step="1"
          min="1" style="width:90px" value="${stake}"
          placeholder="${cfg.stake_usd}"></td>
    </tr>`;
  }).join('') || '<tr><td colspan="6" class="no-data">没有资产</td></tr>';

  for (const cb of document.querySelectorAll('#rows input[data-k="enabled"]')) {
    cb.addEventListener('change', () => {
      const state = cb.closest('tr').querySelector('[data-role="state"]');
      if (!state) return;
      state.textContent = cb.checked ? '启用' : '关闭';
      state.className = 'pill ' + (cb.checked ? 'green' : '');
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
    assets[asset] = {
      enabled,
      sides,
      entry_thresh: entry,
      tp_pct: tpPct / 100,
      stop_loss_pct: 0.0,
      stake_usd: stake,
    };
  }
  return {assets};
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
  if (!confirm('恢复为 config.toml 的默认值？\n默认只启用 BTC。')) return;
  $('msg').textContent = '恢复中...';
  try {
    const r = await fetch('/api/config/reset', {method: 'POST'});
    const data = await r.json().catch(() => ({}));
    if (!r.ok) throw new Error(data.detail || ('HTTP ' + r.status));
    DATA = data;
    render();
    $('msg').textContent = '✅ 已恢复默认（仅 BTC）';
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
