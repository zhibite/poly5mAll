"""Per-asset runtime configuration page (``/config``)."""
from __future__ import annotations

from src.web.ui import render_page

_BODY = r"""
<div class="page-head">
  <div>
    <h1>策略配置</h1>
    <div class="page-sub">7 个币的 5m 参数 · 默认只启用 BTC · 运行时覆盖持久化到 data/runtime_config.json</div>
  </div>
  <div class="head-meta">
    配置版本 <b id="version">--</b><br>
    <span class="mono" id="runtime-path">--</span>
  </div>
</div>

<div class="cards">
  <div class="card"><span>模式</span><strong id="mode">--</strong></div>
  <div class="card"><span>taker 费率</span><strong id="fee-rate">--</strong></div>
  <div class="card"><span>已启用</span><strong id="enabled-count">--</strong></div>
  <div class="card"><span>全局每笔</span><strong id="global-stake">--</strong></div>
  <div class="card"><span>新开仓</span><strong id="entries">--</strong></div>
</div>

<div id="auth-alert"></div>

<div class="alert">
  <b>生效时机：</b>启用/关闭 <b>立即</b> 阻止/允许新开仓；方向、entry、TP、每笔仓位在
  <b>下一个 5m 窗口</b> 生效。已有仓位始终按原参数止盈/240s 强平，不受关闭影响。
  密钥、端口、费率不在本页，需改 <span class="mono">.env</span> / <span class="mono">config.toml</span>。
</div>

<h2>7 个币的参数</h2>
<div class="table-wrap">
<table>
  <thead><tr>
    <th>启用</th><th>币种</th><th>方向</th>
    <th class="num">entry ≤</th><th class="num">TP %</th><th class="num">每笔 U</th>
    <th>TP 费后 / 笔</th><th>备注</th>
  </tr></thead>
  <tbody id="rows"><tr><td colspan="8" class="no-data">加载中...</td></tr></tbody>
</table>
</div>

<div class="controls">
  <button id="save" class="primary">保存配置</button>
  <button id="reset">恢复默认（仅 BTC）</button>
  <span class="msg" id="msg"></span>
</div>

<div class="alert warn">
  手续费公式：<span class="mono">fee = shares × 0.07 × p × (1-p)</span>。
  若某方向 TP 的净利为负，页面会标红；可关闭该币，或用
  <span class="mono">SKIP_FEE_NEGATIVE=true</span> 让后端跳过这些方向。
</div>
"""

_SCRIPT = r"""
let DATA = null;

const SIDE_LABEL = {UP: 'UP', DOWN: 'DOWN', BOTH: 'BOTH（双向）'};

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
    return `<div style="margin:2px 0">
      <span class="pill ${bad ? 'red' : 'green'}">${escapeHtml(f.side)}</span>
      <span class="${bad ? 'neg' : 'pos'}">${money(f.net_per_trade, 2)}</span>
      <span class="muted">/ ${fmt(f.stake_usd, 0)}U · 盈亏平衡 TP ${be}</span>
    </div>`;
  }).join('');
}

function render() {
  const cfg = DATA;
  $('version').textContent = 'v' + (cfg.version ?? 0);
  $('runtime-path').textContent = cfg.runtime_path || '(not persisted)';
  $('mode').textContent = cfg.mode === 'live' ? '实盘' : '纸面';
  $('mode').className = cfg.mode === 'live' ? 'neg' : 'pos';
  $('fee-rate').textContent = fmt(cfg.taker_fee_rate, 4);
  $('global-stake').textContent = fmt(cfg.stake_usd, 0) + ' U';
  const enabled = (cfg.assets || []).filter((a) => a.enabled).length;
  $('enabled-count').textContent = enabled + ' / ' + (cfg.assets || []).length;
  $('entries').textContent = cfg.paused ? '已暂停' : '允许';
  $('entries').className = cfg.paused ? 'neg' : 'pos';

  if (cfg.web && !cfg.web.auth) {
    $('auth-alert').innerHTML =
      '<div class="alert err"><b>⚠️ 后端未设置 WEB_PASSWORD。</b>' +
      '当前 /api/config 可以被同网络内任何人修改。请在 .env 设置 ' +
      '<span class="mono">WEB_PASSWORD</span> 后重启。</div>';
  } else {
    $('auth-alert').innerHTML = '';
  }

  $('rows').innerHTML = (cfg.assets || []).map((a) => {
    const stake = a.stake_usd === null || a.stake_usd === undefined
      ? '' : a.stake_usd;
    return `<tr data-asset="${escapeHtml(a.asset)}">
      <td><input type="checkbox" data-k="enabled" ${a.enabled ? 'checked' : ''}></td>
      <td><b>${escapeHtml(a.asset)}</b></td>
      <td>
        <select data-k="sides" style="width:120px">
          <option value="DOWN" ${sideValue(a) === 'DOWN' ? 'selected' : ''}>DOWN</option>
          <option value="UP" ${sideValue(a) === 'UP' ? 'selected' : ''}>UP</option>
          <option value="BOTH" ${sideValue(a) === 'BOTH' ? 'selected' : ''}>BOTH</option>
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
      <td>${feeCell(a)}</td>
      <td>${a.fee_negative
        ? '<span class="badge red">TP 费后为负</span>'
        : '<span class="badge green">TP 费后为正</span>'}</td>
    </tr>`;
  }).join('') || '<tr><td colspan="8" class="no-data">没有资产</td></tr>';
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
    // Do not clobber unsaved edits: only refresh the read-only header.
    $('version').textContent = 'v' + (cfg.version ?? 0);
    $('entries').textContent = cfg.paused ? '已暂停' : '允许';
    $('entries').className = cfg.paused ? 'neg' : 'pos';
  } catch (e) { /* keep the page usable */ }
}, 5000);
"""

CONFIG_HTML = render_page("策略配置", "/config", _BODY, _SCRIPT)
