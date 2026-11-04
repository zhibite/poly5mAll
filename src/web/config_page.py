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
    <span class="mono" id="runtime-path">--</span>
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
    <div class="stat-sub">官方公式 fee = shares × rate × p × (1-p)</div>
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
  <b>下一个 5m 窗口</b> 生效。已有仓位始终按原参数止盈/240s 强平，不受关闭影响。
  密钥、端口、费率不在本页，需改 <span class="mono">.env</span> /
  <span class="mono">config.toml</span>。
</div>

<div class="config-grid" id="cards">
  <div class="no-data" style="grid-column:1/-1">加载中...</div>
</div>

<div class="save-bar">
  <button id="save" class="btn btn-primary">保存配置</button>
  <button id="reset" class="btn btn-ghost">恢复默认（仅 BTC）</button>
  <span class="msg" id="msg"></span>
</div>

<div class="alert warn" style="margin-top:18px">
  手续费公式：<span class="mono">fee = shares × 0.07 × p × (1-p)</span>。
  若某方向 TP 的净利为负，卡片会标红；可关闭该币，或用
  <span class="mono">SKIP_FEE_NEGATIVE=true</span> 让后端跳过这些方向。
</div>
"""

_SCRIPT = r"""
let DATA = null;

function sideValue(a) {
  const sides = (a.sides || []).map((s) => String(s).toUpperCase());
  if (sides.includes('UP') && sides.includes('DOWN')) return 'BOTH';
  return sides[0] || 'DOWN';
}

function feeCell(a) {
  const fees = a.fees || [];
  if (!fees.length) return '<span class="muted">--</span>';
  return fees.map((f) => {
    const bad = !!f.fee_negative;
    const be = f.breakeven_tp === null
      ? 'n/a' : (Number(f.breakeven_tp) * 100).toFixed(1) + '%';
    return `<div class="fee-row">
      <span class="pill ${bad ? 'red' : 'green'}">${escapeHtml(f.side)}</span>
      <span class="${bad ? 'red' : 'green'}">${money(f.net_per_trade, 2)}</span>
      <span class="muted">/ ${fmt(f.stake_usd, 0)}U · 盈亏平衡 TP ${be}</span>
    </div>`;
  }).join('');
}

function render() {
  const cfg = DATA;
  $('version').textContent = 'v' + (cfg.version ?? 0);
  $('runtime-path').textContent = cfg.runtime_path || '(not persisted)';
  $('mode').textContent = cfg.mode === 'live' ? '实盘' : '纸面';
  $('mode').className = 'stat-value ' + (cfg.mode === 'live' ? 'red' : 'green');
  $('fee-rate').textContent = fmt(cfg.taker_fee_rate, 4);
  $('global-stake').textContent = fmt(cfg.stake_usd, 0) + ' U';
  const enabled = (cfg.assets || []).filter((a) => a.enabled).length;
  $('enabled-count').textContent = enabled + ' / ' + (cfg.assets || []).length;
  $('entries').textContent = cfg.paused ? '已暂停' : '允许';
  $('entries').className = 'stat-value ' + (cfg.paused ? 'red' : 'green');

  if (cfg.web && !cfg.web.auth) {
    $('auth-alert').innerHTML =
      '<div class="alert err"><b>⚠️ 后端未设置 WEB_PASSWORD。</b>' +
      '当前 /config 和 /api/config 可被同网络任何人访问。请在 .env 设置 ' +
      '<span class="mono">WEB_PASSWORD</span> 后重新部署。</div>';
  } else {
    $('auth-alert').innerHTML = '';
  }

  $('cards').innerHTML = (cfg.assets || []).map((a) => {
    const stake = a.stake_usd === null || a.stake_usd === undefined
      ? '' : a.stake_usd;
    const on = !!a.enabled;
    return `<div class="asset-card" data-asset="${escapeHtml(a.asset)}">
      <div class="asset-head">
        <div>
          <span class="asset-name">${escapeHtml(a.asset)}</span>
          <span class="pill ${on ? 'green' : ''}">${on ? '启用' : '关闭'}</span>
        </div>
        <label class="switch-row">
          <input type="checkbox" data-k="enabled" ${on ? 'checked' : ''}>
          启用
        </label>
      </div>
      <div class="asset-fields">
        <div class="field">
          <label>方向</label>
          <select data-k="sides">
            <option value="DOWN" ${sideValue(a) === 'DOWN' ? 'selected' : ''}>DOWN</option>
            <option value="UP" ${sideValue(a) === 'UP' ? 'selected' : ''}>UP</option>
            <option value="BOTH" ${sideValue(a) === 'BOTH' ? 'selected' : ''}>BOTH（双向）</option>
          </select>
        </div>
        <div class="field">
          <label>entry ≤</label>
          <input type="number" data-k="entry_thresh" step="0.01"
                 min="0.01" max="0.99" value="${a.entry_thresh}">
        </div>
        <div class="field">
          <label>TP %</label>
          <input type="number" data-k="tp_pct" step="0.5"
                 min="0.1" max="1000"
                 value="${(Number(a.tp_pct) * 100).toFixed(1)}">
        </div>
        <div class="field">
          <label>每笔 U <span class="help">留空=全局 ${cfg.stake_usd}U</span></label>
          <input type="number" data-k="stake_usd" step="1" min="1"
                 value="${stake}" placeholder="${cfg.stake_usd}">
        </div>
      </div>
      <div class="fee-line">${feeCell(a)}</div>
    </div>`;
  }).join('') || '<div class="no-data" style="grid-column:1/-1">没有资产</div>';
}

function collect() {
  const assets = {};
  for (const card of document.querySelectorAll('.asset-card[data-asset]')) {
    const asset = card.getAttribute('data-asset');
    const get = (k) => card.querySelector(`[data-k="${k}"]`);
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
