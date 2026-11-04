# 06.poly5mAll — 7 币 5 分钟市场策略 1（Rebound）

在 Polymarket 的 **BTC / ETH / BNB / SOL / DOGE / XRP / HYPE** 七个 5 分钟
Up/Down 市场上，按 `docs/all_5m_strategy1_summary.md` §2 的生产参数运行
“0–60s 观察 → 60–240s TP → 240s 强平”的反弹策略。

价格层参考 `../02.poly1h`：Gamma 发现市场、CLOB 市场 WebSocket 提供
best bid/ask 与 last trade、CLOB `/midpoint` 每 2 秒作为稳定兜底。
执行层默认 **paper**（对实时订单簿撮合），也支持真实 CLOB FAK 下单。

内置 **Web 后端（端口 8033）**：控制台、7 币策略配置页、交易记录/盈利统计页。
默认只启用 **BTC**，其余币可在 `/config` 页面开启或关闭。

---

## 1. 策略时间线

```
t = 0                5m 市场开始（slug = {asset}-updown-5m-{epoch}）
0 <= t <= 60 s       观察；该方向价格首次 <= ENTRY_THRESH -> 买入
60 < t < 240 s       价格首次 >= 实际成交价 * (1 + TP_PCT) -> 卖出止盈
t >= 240 s           仍未止盈 -> 以市价 FAK 强平（文档的“240s SL 单点判定”）
t > 295 s            仍未卖出 -> 标记 SETTLE_PENDING，按 Gamma 结算价记账
```

- 入场窗口内**首次触及**即入场；每方向每个窗口最多一笔。
- 入场后不再加仓、不在同窗口重新入场。
- TP 只在 60s 之后判断：即使 30s 已到目标价，也等到 60s 再卖（文档口径）。
- 未止盈的仓位在 240s 起每秒重试强平，直到 295s。
- HYPE 可配置 `SIDES="BOTH"`，UP/DOWN 各跑一笔；默认按文档 §2 只跑 DOWN。

### 观察价止盈（默认）

实盘/纸面默认 **`TP_EXECUTION_MODE=observed`**：

1. 触发：可成交 bid（无 BBO 时用 mid）≥ `target = fill_price × (1 + TP)`；
2. 执行：FAK 卖出，限价 = `floor(target)`；
3. 成交价 = 当时订单簿的最优 bid。若价格从 0.27 跳到 0.42，就按 **0.42**
   成交，而不是按 0.275 挂限价成交。

`data/trades.jsonl` 里 `tp_price` 是目标价、`exit_price` 是实际观察成交价，
可以直接核对。文档 [观察止盈](docs/观察止盈.md) 说明了 target 限价与观察价
的差异；本项目**不实现**“挂 target 限价（A）”，因为回测里 target 口径远差于
observed。

可选 `TP_EXECUTION_MODE=market` 是文档里的做法 B：mid 穿越 target 后发
可成交 FAK（限价在 bid 下方 2%），能吃到越价，但 bid 没跟上时也可能**低于
target 成交**。默认不用。

## 2. 文档生产参数（`config.toml`）

| 品种 | 方向 | entry | tp% | 文档依据 |
|---|---|---:|---:|---|
| BTC | DOWN | 0.25 | 10% | §2 / §4.1 |
| ETH | DOWN | 0.18 | 15% | §2 / §4.2（旧报告口径） |
| BNB | DOWN | 0.15 | 10% | §2 / §4.4 |
| SOL | DOWN | 0.15 | 20% | §2 / §4.5 |
| DOGE | DOWN | 0.15 | 25% | §2 / §4.6 |
| **XRP** | **UP** | 0.10 | 25% | §2 / §4.3（唯一 UP 优势） |
| HYPE | DOWN | 0.30 | 10% | §2 / §4.7 |

单笔仓位固定 **10 U**（`EXECUTION.STAKE_USD`）。
**默认只启用 BTC**（`[ASSETS.BTC] ENABLED=true`，其余为 false）；在
`http://<host>:8033/config` 可以逐币开启/关闭并修改参数。

> ⚠️ **不要随手把 XRP 改成 DOWN**。文档里 XRP 是唯一“UP 强于 DOWN”的品种；
> 反过来做是结构性亏损。其余六个品种的文档结论都是 DOWN-only。

> 备注：`docs/btc_5m_strategy1_recheck_2026-11-04.md` 在最近 6 个月数据上
> 发现 BTC 的 `0.30/15%` 比文档的 `0.25/10%` 更好。本项目按用户要求使用
> 汇总文档 §2 的参数；若要改用 recheck 参数，只改 `[ASSETS.BTC]` 即可。

## 3. 先看手续费：这是本策略最大的现实风险

Polymarket 加密类 taker 手续费（官方公式）：

```
fee = shares × 0.07 × p × (1 - p)
```

maker 为 0；5m 策略用 FAK 主动成交，按 taker 计。文档回测**没有扣手续费**。
`python -m src.main --check` 会打印每个品种的 TP 经济性：

| 品种/方向 | entry | tp% | TP 毛利/份 | 往返手续费/份 | 净利/份 | 10U 净利 |
|---|---:|---:|---:|---:|---:|---:|
| BTC DOWN | 0.25 | 10% | 0.0250 | 0.0271 | **-0.0021** | **-0.083 U** |
| ETH DOWN | 0.18 | 15% | 0.0270 | 0.0218 | +0.0052 | +0.288 U |
| BNB DOWN | 0.15 | 10% | 0.0150 | 0.0186 | **-0.0036** | **-0.238 U** |
| SOL DOWN | 0.15 | 20% | 0.0300 | 0.0193 | +0.0107 | +0.716 U |
| DOGE DOWN | 0.15 | 25% | 0.0375 | 0.0196 | +0.0179 | +1.194 U |
| XRP UP | 0.10 | 25% | 0.0250 | 0.0140 | +0.0110 | +1.104 U |
| HYPE DOWN | 0.30 | 10% | 0.0300 | 0.0302 | **-0.0002** | **-0.006 U** |

> 表中按 entry 阈值和精确 TP 价计算。实盘成交价按 0.01 tick 取整，入场
> 可能略低、TP 可能高一档，实际净利会与表格不同（通常略好）；这是保守估算。

**BTC / BNB / HYPE 的 TP 目标在 0.07 费率下是“赢了也亏一点”**。这不代表
整个策略一定亏（240s 强平那一半仓位另有盈亏），但说明文档的 TP 率优势
被手续费吃掉了一大截。实盘前建议：

1. 先用 paper 模式（默认扣 0.07 费率）跑至少一周，看 `trades.jsonl` 的净 PnL；
2. 若要严格按文档参数交易，可设 `SKIP_FEE_NEGATIVE=true`，只跑
   ETH / SOL / DOGE / XRP 四个 TP 费后为正的方向；
3. 做文档对照研究时可设 `TAKER_FEE_RATE=0`，但**不要**用这个数字评估实盘收益；
4. 真实费率以 CLOB `/fee-rate` 为准，配置里的 0.07 是保守上界。

## 4. 快速开始

```bash
cd D:/05.project2/3.polymarket/06.poly5mAll
python -m pip install -r requirements.txt

# 1) 只校验配置 + 打印手续费表，不连接网络
python -m src.main --check

# 2) 探测当前 5m 市场（需要能访问 Gamma / CLOB）
python -m src.main --probe

# 3) paper 运行（默认；不碰真实资金）
python -m src.main

# 4) 只跑 BTC/ETH 10 分钟
python -m src.main --assets BTC,ETH --duration 600

# 5) 实盘（先填好 .env 凭据）
python -m src.main --live
```

启动后打开：

- `http://127.0.0.1:8033/` — 控制台（状态、今日/累计 PnL、暂停/平仓）
- `http://127.0.0.1:8033/trades` — 交易记录、分币种/按日盈利统计
- `http://127.0.0.1:8033/config` — 7 币参数配置（默认只启用 BTC）

Docker（默认 paper；`data/` 与 `config.toml` 以卷挂载）：

```bash
cp .env.example .env   # 按需修改
docker compose up -d --build
docker compose logs -f
```

实盘需要 **同时**满足：

- `.env` 里 `PAPER_TRADING=false` 且 `LIVE_ENABLED=true`（或命令行 `--live`）；
- **`WALLET_PRIVATE_KEY`**（每笔订单都要钱包签名；只填 L2 API 凭据无法下单）；
- 可选 `API_KEY/API_SECRET/API_PASSPHRASE`；不填时启动时会从私钥派生。

## 5. Web 后端（端口 8033）

后端随 `python -m src.main` 一起启动（`[WEB] ENABLED=true`），不需要单独进程。

### 5.1 页面

| 路径 | 内容 |
|---|---|
| `/` | 控制台：模式、运行时长、今日/累计 PnL、胜率、盈亏比、手续费；7 币当前窗口/持仓；暂停新开仓；全部市价平仓 |
| `/trades` | 交易记录：每笔明细（入场/出场/费用/净 PnL/持有时间）、按币种统计、按日统计、筛选与分页 |
| `/config` | 策略配置：7 个币的启用开关、方向（UP/DOWN/BOTH）、entry、TP%、每笔 U；TP 费后/笔提示；恢复默认（仅 BTC） |

### 5.2 配置生效时机

- **启用/关闭**：立即影响新开仓；关闭后已有仓位仍按原参数止盈/240s 强平。
- **方向 / entry / TP / 每笔 U**：下一个 5m 窗口生效（策略在窗口开始时读取）。
- 配置保存在 `data/runtime_config.json`，重启不丢；`POST /api/config/reset`
  或页面“恢复默认”会删掉该文件并回到 `config.toml` 的默认值（仅 BTC 开启）。
- 密钥、端口、费率不在页面里，需改 `.env` / `config.toml`。

### 5.3 JSON API

| 方法 | 路径 | 说明 |
|---|---|---|
| GET | `/healthz` | 存活检查（免鉴权） |
| GET | `/api/status` | runner 完整快照（资产、窗口、持仓、浮盈、daily） |
| GET | `/api/stats` | 累计/今日/分币种/按日/按 reason 的盈亏、胜率、盈亏比、最大回撤 |
| GET | `/api/trades` | `limit/offset/asset/reason/mode` 筛选 + 分页 |
| GET | `/api/config` | 运行时 7 币参数 + 每方向 TP 费后收益 |
| PUT | `/api/config` | `{"assets": {"BTC": {...}}}` 更新并持久化 |
| POST | `/api/config/reset` | 恢复默认（仅 BTC） |
| POST | `/api/pause` | `{"paused": true}` 暂停/恢复新开仓 |
| POST | `/api/flatten` | `{"confirm": "FLATTEN"}` 全部市价平仓 |

### 5.4 鉴权

默认 `WEB_PASSWORD` 为空（内网/本机方便）。**对外暴露前必须在 `.env`
设置 `WEB_PASSWORD`**。设置后：

- 访问 `/`、`/trades`、`/config` 会跳转到 **`/login` 登录页**（不是浏览器
  Basic 弹框）；
- 登录成功后写入 `poly5m_session` HttpOnly Cookie，默认 7 天有效
  （`WEB_SESSION_TTL_S`）；
- 导航栏显示用户名和“退出”；会话保存在进程内存，重启后需要重新登录；
- API 未登录返回 JSON `401`，HTML 页面 303 跳转登录页；
- `/healthz`、`/login`、`/api/auth/*` 免鉴权。

样式参考 `../02.poly1h` 的紫色暗色主题。

## 6. 目录

```
config.toml                 策略/执行/价格层参数（含 7 个品种的文档参数）
.env.example                凭据与运行开关
src/
  config.py                 配置加载/校验 + 费用诊断
  models.py                 Side / MarketWindow / Quote / Position / Fill / TradeRecord
  economics.py              官方费率、TP 经济性、breakeven TP
  market/pm_client.py       Gamma 发现 + CLOB midpoint/orderbook/tick/fee/resolution
  market/market_ws.py       CLOB 市场 WS + PriceCache（BBO/last trade）
  market/service.py         当前/下一个 5m 窗口管理
  execution/broker.py       PaperBroker（走实时订单簿）+ LiveBroker
  execution/clob_client.py  精简版 py-clob-client-v2 FAK 封装
  strategy/rebound.py       纯状态机：入场/TP/240s 强平/结算
  runtime_config.py         7 币运行时参数覆盖（data/runtime_config.json）
  runner.py                 7 个 asset 任务 + 发现/中间价/状态/结算循环
  store.py                  state.json / trades.jsonl / status.json
  web/app.py                FastAPI 路由 + 鉴权
  web/server.py             同事件循环内的 uvicorn（8033）
  web/dashboard.py          控制台页面
  web/trades_page.py        交易记录/盈利统计页面
  web/config_page.py        7 币策略配置页面
  web/stats.py              交易统计（PnL/胜率/盈亏比/回撤/分币种/按日）
  main.py                   CLI 入口
tests/                      46 个离线测试（策略、paper 撮合、状态机、配置、FAK 解析、Web API/登录、观察价止盈）
docs/strategy_5m_all.md     详细设计与口径
```

运行后 `data/` 下会有：

| 文件 | 说明 |
|---|---|
| `state.json` | 未平仓仓位 + 每日已实现 PnL，重启恢复用 |
| `trades.jsonl` | 每笔完整交易（入场/出场/费用/净 PnL） |
| `status.json` | 每 5 秒刷新的运行快照 |
| `runtime_config.json` | Web 配置页保存的 7 币运行时覆盖（不存在时用 config.toml） |
| `logs/bot_YYYYMMDD.log` | 日志（默认保留 14 天） |
| `STOP` | 创建该文件即暂停**新开仓**（已有仓位仍会正常止盈/强平） |

## 7. 价格层口径（参考 02.poly1h）

- **市场发现**：`GET gamma-api/events?slug={asset}-updown-5m-{epoch}`，
  同时查询当前、上一个、下一个窗口；提前 60s 预发现下一个窗口并订阅 token。
  5m slug 可能使用 ticker 或全名（`sol`/`solana`、`doge`/`dogecoin`、
  `hype`/`hyperliquid` 等），客户端会按别名探测并缓存命中的前缀。
- **决策参考价**：优先 WS best bid/ask 中点；WS 不新鲜时用 CLOB `/midpoint`
  （每 2s 轮询）；再不行用 last trade。超过 `STALE_PRICE_MAX_AGE_S` 不允许开仓。
- **可成交价**：买入用真实订单簿 ask 阶梯（`/orderbook`）或 FAK 限价；
  止盈用 bid；240s 强平用当前 bid 下方 2% 的限价（`SLIPPAGE_PCT`）以确保成交。
- **paper 撮合**：按 `/orderbook` 逐档吃单，受 `limit` 限制；WS top-of-book
  只作为 REST 快照失败时的兜底。paper 也扣 0.07 taker 费，因此比文档的
  零费率回测更保守。

与文档回测的差异：文档用 1s 价格序列、零费用、假设按观察价成交；本项目
用实时 BBO/中间价触发、按订单簿可成交价成交、扣费、允许部分成交。因此
**信号数和绝对收益不应与文档逐字比较**。

## 8. 风险控制

| 配置 | 默认 | 作用 |
|---|---|---|
| `PAPER_TRADING` | true | 默认 paper；实盘需双开关 |
| `MAX_DAILY_LOSS_USD` | 0（关闭） | 当日净亏损达到该值后停止新开仓 |
| `PAUSE_NEW_ENTRIES` | false | 暂停新开仓；已有仓位照常管理 |
| `KILL_SWITCH_FILE` | `data/STOP` | 文件存在即暂停新开仓 |
| `MAX_SPREAD_ABS` | 0（关闭） | 点差超过该值时跳过入场 |
| `SKIP_FEE_NEGATIVE` | false | 跳过 TP 费后为负的方向 |
| `TP_EXECUTION_MODE` | observed | observed=按最优 bid 观察价成交；market=mid 触发后市价追价，可能低于 target |
| `FLATTEN_ON_EXIT` | false | 优雅退出时尽力卖出未平仓仓位 |
| `/api/pause` / 页面按钮 | — | 运行时暂停新开仓；已有仓位照常管理 |
| `/config` 启用开关 | 仅 BTC | 关闭某币只停新开仓，不影响已有仓位离场 |
| `WEB_PASSWORD` | 空 | 登录页密码；对外暴露前必须设置 |
| `WEB_SESSION_TTL_S` | 604800 | 登录 Cookie 有效期（秒）；重启后失效 |

240s 强平失败时不会“假装平仓”：仓位会保留、状态变 `SETTLE_PENDING`，
并在窗口结束后用 Gamma 的 0/1 结算价记账。实盘赢方 token 的赎回可能仍需
在 Polymarket 界面手动完成，详见 `docs/strategy_5m_all.md`。

## 9. 测试

```bash
python -m pytest -q
# 46 passed
```

测试覆盖：文档参数加载/校验、费用公式与 breakeven、入场只在 0–60s、
TP 只在 60s 后且用可成交 bid、240s 强平、部分成交、结算、paper 订单簿撮合、
价格缓存新鲜度优先级、当前/下一窗口发现与边界推进、state 持久化、
AssetRunner 端到端入场→TP/强平、重启后过期仓位结算、FAK 回报解析、
运行时配置覆盖/持久化、Web API（状态/配置/交易/统计/暂停/鉴权）。

## 10. 已知限制

1. **未在真实网络环境验证**：当前开发环境的 Gamma/CLOB 出口被阻断；
   代码中的价格层按 02.poly1h 的同一套 API 实现，部署到可访问 Polymarket
   的机器后请先跑 `--probe` 和 paper。
2. **5m 市场流动性薄**：10U 的单子可能部分成交或多次重试；paper 已按真实
   订单簿模拟，但实盘滑点仍可能更大。
3. **文档参数是 60 天样本的产物**：币圈波动结构变化后需要重新回测；
   本项目按文档参数实现，不做自动再优化。
4. **7% taker 费**是官方公式的保守值；不同市场/账户的实际费率可能不同，
   实盘第一周请用 `trades.jsonl` 对照真实扣费。
5. **Web 后端默认无鉴权**：`WEB_PASSWORD` 为空时，同网络内任何人都能修改配置
   或触发平仓。对公网暴露前务必设置 `WEB_PASSWORD`，并优先只绑定内网/本机。
6. **配置页不是热重载所有参数**：启用/关闭立即生效，entry/TP/方向/每笔 U
   在下一个 5m 窗口生效；费率/端口/密钥仍需改配置文件并重启。
