# 7 币 5m 策略 1 设计与运行口径

> 目标：把 `docs/all_5m_strategy1_summary.md` 的“反弹策略”落到 7 个
> Polymarket 5m Up/Down 市场的实时交易运行时。
> 价格层参考 `../02.poly1h/src/market/{pm_client,market_ws}.py`。
> 代码入口：`src/main.py`；核心状态机：`src/strategy/rebound.py`。

---

## 1. 市场与窗口模型

每个品种每 300 秒有一个独立的 Up/Down 市场：

```
slug = {asset.lower()}-updown-5m-{start_epoch}
start_epoch = floor(now / 300) * 300
```

- 一个窗口有 UP / DOWN 两个 token，价格都在 (0, 1)。
- 窗口内 token 是独立可交易资产；PnL 只看 token 价格，不看“方向是否猜对”。
- 文档的回测把 UP、DOWN 分开统计；本运行时也为每个方向维护独立的
  `SideRuntime`，所以 HYPE 可以同时跑两边（`SIDES="BOTH"`）。

## 2. 时间轴

```
t = 0                 窗口开始
0 <= t <= 60          入场窗口：该方向中间价首次 <= ENTRY_THRESH 时买入
60 <= t < 240         止盈窗口：价格首次 >= 实际成交价 * (1 + TP_PCT) 时卖出
240 <= t <= 295       强平窗口：仍未止盈的仓位以 FAK 市价卖出，每秒重试
t > 295               强平截止：标记 SETTLE_PENDING，等待 Gamma 结算
```

关键点：

1. **TP 不在 60s 前触发**。即使 30s 就到了目标价，也等到 60s 才卖；
   这是文档“60–240s TP”的字面口径。
2. **入场只在 60s 前**。61s 即使价格新低也不再开仓。
3. **240s 是“单点判定”**。文档把未止盈仓位按 240s 的价格平掉；本项目
   从 240s 起持续尝试市价卖出，最多到 295s（留 5s 结算缓冲）。
4. **每方向每窗口最多一笔**。平仓后不重新入场。

## 3. 状态机

`SideRuntime` 的状态：

| 状态 | 含义 |
|---|---|
| `WAITING` | 等待入场条件 |
| `ENTERING` | FAK 买单在途 |
| `HOLDING` | 已成交，等待 TP 或 240s |
| `EXITING` | FAK 卖单在途 |
| `CLOSED` | 已平仓/已结算 |
| `SETTLE_PENDING` | 295s 仍未卖出，等待结算 |

每个 tick（默认 250ms）按以下顺序判断：

```
if 无仓位:
    entry_block_reason(...) == None -> 买入
else:
    tp_due(...)          -> 止盈卖出
    force_exit_due(...)  -> 240s 强平
    deadline_passed(...) -> SETTLE_PENDING
```

### 3.1 入场条件

同时满足：

- 状态 `WAITING`；
- `0 <= t <= 60`；
- 距上次尝试 >= `ENTRY_RETRY_S`（默认 1s）；
- 有报价，且报价年龄 <= `STALE_PRICE_MAX_AGE_S`（默认 5s）；
- `quote.mid <= ENTRY_THRESH`；
- 如果开启 `MAX_SPREAD_ABS`，点差不超限；
- 风控允许新开仓（未暂停、无 STOP 文件、未触当日亏损上限）。

买入执行：

- 触发价：WS BBO 中点（没有 BBO 时用 REST midpoint / last trade）；
- 限价：`floor_tick(ENTRY_THRESH, 0.01)`；
- paper：按 `/orderbook` 的 ask 阶梯用 10U 预算逐档吃单，只能吃到限价；
- live：`FAK BUY`，amount = 10U，price = 限价。

> 为什么触发用 mid、成交用 ask：文档的价格序列没有订单簿，只能用中间价
> 定义“触及”；实盘必须按可成交的 ask 成交。若 mid 已到阈值但 ask 仍在
> 阈值上方，FAK 不会成交，下一 tick 重试。

### 3.2 止盈条件

同时满足：

- 状态 `HOLDING`；
- `60 <= t < 240`；
- 距上次尝试 >= `EXIT_RETRY_S`；
- 有报价且年龄 <= `EXIT_STALE_PRICE_MAX_AGE_S`；
- 可成交价（优先 bid，缺失时用 mid）>= `position.tp_price`。

卖出执行：

- 限价：`floor_tick(tp_price, 0.01)`（卖单限价是“最低可接受价”，
  向下取整保证成交优先；实际成交价由 bid 决定，触发时 bid 已 >= target）；
- paper：按 bid 阶梯卖出全部剩余份额；
- live：`FAK SELL`，amount = 剩余份额，price = 限价。

`tp_price = 实际成交均价 * (1 + TP_PCT)`，不是文档的 entry_thresh。

### 3.3 240s 强平

- 从 `t >= 240` 起，每秒尝试一次；
- 限价 = `max(0.01, floor(bid * (1 - SLIPPAGE_PCT/100)))`，
  默认滑点 2%；bid 缺失时用 0.01（直接穿价）；
- paper 按 bid 阶梯卖出；live 用 FAK SELL；
- 成功则 `reason="time_exit"`；
- `t > 295` 仍未卖出 -> `SETTLE_PENDING`，由结算循环按 Gamma 的 0/1 结算价记账。

### 3.4 部分成交

FAK 可能只成交一部分：

- 买入：按实际成交 shares / usdc / price 建仓，仓位可能小于 10U；
- 卖出：每次减少 `remaining_shares`，累计 `exit_proceeds` / `exit_fees`；
- 只有剩余份额为 0 才生成 `TradeRecord` 并进入 `CLOSED`；
- 未卖完的仓位继续按 TP / 强平规则处理。

## 4. 费用与 PnL

### 4.1 官方公式

```
fee = shares * fee_rate * p * (1 - p)
```

- 加密类 `fee_rate = 0.07`；maker 为 0；本项目全部是 taker FAK。
- paper 默认也扣费（`INCLUDE_FEES=true`），否则 paper 结果会系统性高估。

### 4.2 单笔 PnL

```
entry_cost   = 买入 share notional（不含 fee）
entry_fee    = 买入手续费
exit_proceeds= 卖出所得 USDC
exit_fee     = 卖出手续费
pnl_gross    = exit_proceeds - entry_cost
pnl_net      = pnl_gross - entry_fee - exit_fee
```

`trades.jsonl` 同时记录 gross / net / fees，便于对照文档的零费率结果。

### 4.3 为什么 BTC/BNB/HYPE 的 TP 费后为负

以 BTC 0.25 / 10% 为例：

```
TP 毛利/份       = 0.25 * 10%              = 0.0250
买入费/份        = 0.07 * 0.25 * 0.75      = 0.0131
卖出费/份(0.275) = 0.07 * 0.275 * 0.725    = 0.0140
净利/份          = 0.0250 - 0.0271         = -0.0021
```

10U 仓位 ≈ 40 份 -> TP 命中时约 **-0.08U**。BNB 0.15/10% 约 -0.24U，
HYPE 0.30/10% 约 -0.006U。ETH/SOL/DOGE/XRP 的 TP 费后为正。

表中按 entry 阈值和精确 TP 价计算；实盘按 0.01 tick 成交，入场可能略低、
TP 可能高一档，实际净利会与表格不同（通常略好）。

启动时 `--check` 会打印完整表格，并在 `SKIP_FEE_NEGATIVE=true` 时跳过
费后为负的方向。是否接受这些“赢也微亏”的 TP 由操作者决定；运行时
不会擅自改文档参数。

## 5. 价格层

### 5.1 发现

`MarketService` 每 `MARKET_DISCOVERY_S`（默认 2s）调用 Gamma：

```
GET {gamma}/events?slug={asset}-updown-5m-{epoch}
```

同时查询 `base-300 / base / base+300` 三个窗口，并发发出；这样：

- 当前窗口尽快可用；
- 下一个窗口在开始前 60s 内被发现并订阅 token；
- 上一个窗口保留给迟到强平。

5m event slug 在不同资产/时期可能使用 ticker 或全名。客户端按
`btc/bitcoin`、`eth/ethereum`、`sol/solana`、`doge/dogecoin`、
`hype/hyperliquid` 等别名依次探测，并缓存第一个命中的前缀，后续只查
缓存前缀；全部失败且 HTTP 不可达时提前停止，避免在网络故障时卡住
发现循环。

### 5.2 实时价格

三条来源，按新鲜度择优：

1. **WS BBO**：`best_bid` / `best_ask` 事件，`mid = (bid+ask)/2`；
2. **REST midpoint**：`GET /midpoint?token_id=...`，每 2s 轮询；
3. **last trade**：WS `last_trade_price` / `price_change` 的成交价。

`Quote` 额外带 `bid` / `ask`，只有 BBO 本身足够新鲜（默认 5s）才暴露；
避免用 10 秒前的 bid 触发止盈。

### 5.3 为什么不用 Binance 现货价

策略 1 交易的是 Polymarket token 的概率价格，不是标的现货方向。
文档的回测也是 token 价格序列；因此运行时不需要 Binance feed。
（02.poly1h 的 Binance feed 服务于 1h martingale 的 drift/baseline 门，
与本策略无关。）

## 6. 执行层

### 6.1 PaperBroker（默认）

- 每次买入/卖出拉取一次 `/orderbook`；
- 买入用 `walk_budget(asks, 10U, limit)`，卖出用 `walk_shares(bids, shares, limit)`；
- 尊重档位深度，因此会部分成交；
- REST 快照失败时退回 WS top-of-book（仅一档，size 视为无限）；
- 手续费按官方公式扣除；
- 维护 `holdings`，支持重启后重新登记。

### 6.2 LiveBroker

- 复用 `py-clob-client-v2`，精简为 `place_fak_buy` / `place_fak_sell` /
  `get_token_balance`；
- L2 凭据可直接填，或用 `WALLET_PRIVATE_KEY` 在启动时派生；
- FAK 买入 amount 以 USDC 计，FAK 卖出以 shares 计；
- 解析 `takingAmount` / `makingAmount`，兼容 6 位 base units；
- 手续费按配置费率计算并写入成交记录。

### 6.3 为什么全是 FAK

5m 窗口只有 300s，挂在订单簿上的 GTD 可能跨越窗口结束；而且文档的回测
假设“触价即成交”，对应主动成交。FAK 保证要么立即成交，要么不占用资金。
如果未来要做 maker 版本，需要单独设计撤单/心跳/窗口边界，不能直接复用
1h bot 的 GTD 逻辑。

## 7. 持久化与重启恢复

### 7.1 文件

| 文件 | 内容 |
|---|---|
| `data/state.json` | 未平仓仓位 + 每日 DailyStats |
| `data/trades.jsonl` | 每笔完整交易 |
| `data/status.json` | 5 秒快照（供外部监控） |
| `data/logs/bot_*.log` | 日志 |

`state.json` / `status.json` 采用 tmp + `os.replace` 原子写。

### 7.2 重启

启动时 `StateStore.load()` 恢复仓位：

- 窗口仍活跃：加入对应 `AssetRunner.pending_positions`，绑定窗口时接管；
  - live：先查链上 token balance，balance=0 则丢弃，balance 与记录不符则以
    链上余额为准；
  - paper：重新登记 `PaperBroker.holdings`；
- 窗口已结束：加入 `_stale`，由结算循环按 Gamma 结果记账。

### 7.3 结算

`SETTLE_PENDING` / 启动时已过期的仓位，每 10s 查询：

```
GET {gamma}/events?slug=...
```

读取 `outcomes` + `outcomePrices`。当价格已变为 0/1 时：

- 我方 token 赢：payout = 1.0/share；
- 输：payout = 0；
- 生成 `reason="settle"` 的 TradeRecord。

实盘注意：结算记账不等于 USDC 已到账。赢方 token 的赎回通常需要在
Polymarket 界面完成（或自行接入 redeem 交易）；本项目只做账务记录。

## 8. Web 后端与运行时配置

### 8.1 进程模型

FastAPI + uvicorn 跑在策略的同一个事件循环里，端口 **8033**
（`[WEB] PORT=8033`）。uvicorn 的 signal capture 被禁用，SIGINT/SIGTERM
仍由 bot 统一处理。端口被占用时只记录错误，交易循环继续。

### 8.2 页面

| 路径 | 内容 |
|---|---|
| `/` | 控制台：模式、运行时长、今日/累计 PnL、胜率、盈亏比、手续费；7 币窗口/持仓/浮盈；暂停；全部平仓 |
| `/trades` | 交易明细（分页 + asset/reason/mode 筛选）、分币种统计、按日统计 |
| `/config` | 7 币启用开关、方向、entry、TP%、每笔 U；TP 费后收益；恢复默认 |

### 8.3 运行时参数

- 基线仍是 `config.toml`；页面修改写入 `data/runtime_config.json`。
- **默认只启用 BTC**（`config.toml` 中其余 `ENABLED=false`）。
- 启用/关闭：下一次入场判断立即生效；关闭不会阻止已有仓位的 TP/240s 强平。
- 方向 / entry / TP / 每笔 U：下一个 5m 窗口绑定 `SideRuntime` 时生效。
- 恢复默认会删除 `runtime_config.json`，回到 `config.toml`。
- 校验：entry ∈ (0,1)，entry×(1+TP)<1，TP>0，stake>0，方向必须含 UP/DOWN。

### 8.4 交易统计

`/api/stats` 从 `trades.jsonl` 计算：

- 累计/今日：笔数、胜/负、胜率、净/毛 PnL、手续费、均赢/均亏、盈亏比、
  最大回撤（按平仓顺序）、平均持仓秒数；
- 分币种、分方向、分 reason（tp/time_exit/settle/shutdown）、按日；
- 最佳/最差单笔。

`/api/trades` 支持 `limit/offset/asset/reason/mode`，按 exit_ts 倒序分页。

### 8.5 鉴权与操作

- `WEB_PASSWORD` 非空时启用 HTTP Basic auth；默认空 = 无鉴权（仅适合内网）。
- `POST /api/pause` 暂停新开仓；`POST /api/flatten` 需要
  `{"confirm":"FLATTEN"}` 才会全部市价平仓。
- `/healthz` 免鉴权，供探活。

## 9. 风控

| 机制 | 行为 |
|---|---|
| `PAPER_TRADING` + `LIVE_ENABLED` | 双开关；默认 paper，避免误下真单 |
| `MAX_DAILY_LOSS_USD` | 当日净亏损达到上限后只平仓、不开新仓 |
| `PAUSE_NEW_ENTRIES` | 只停新开仓；TP/强平照常 |
| `KILL_SWITCH_FILE` | 文件存在即停新开仓 |
| `MAX_SPREAD_ABS` | 点差过滤（默认关闭） |
| `SKIP_FEE_NEGATIVE` | 跳过 TP 费后为负的方向 |
| `STALE_PRICE_MAX_AGE_S` | 报价太旧不开仓 |
| `EXIT_STALE_PRICE_MAX_AGE_S` | 出场允许更旧的报价（不能因为数据旧就不止损） |
| `FLATTEN_ON_EXIT` | 优雅退出时尽力平仓 |
| state 持久化 | 重启不丢仓位 |

“新开仓”和“管理已有仓位”是分开的：任何风控开关都不会阻止止盈或 240s 强平。

## 10. 与文档回测的口径差异

| 环节 | 文档回测 | 本运行时 |
|---|---|---|
| 价格序列 | 1s K 线 | WS BBO（事件驱动）+ 2s REST midpoint |
| 入场价 | 首次触及阈值的观察价 | 限价 = 阈值，按 ask 阶梯 FAK 成交 |
| 止盈价 | 首次 >= entry*(1+tp) 的观察价 | bid >= target，按 bid 成交 |
| 240s | 取 240s 前最后样本价 | 从 240s 起 FAK 市价强平，最多到 295s |
| 手续费 | 0 | 0.07 taker（可配 0） |
| 滑点 | 0 | 实际订单簿深度 + 强平 2% 保护 |
| 部分成交 | 无 | 有，按实际份额记账 |
| 数据缺失 | 过滤样本 | 报价太旧则跳过入场 |

因此：**信号数通常更少，绝对收益通常更低，paper 结果比文档更保守。**
这不是实现错误，而是可执行性的代价。

## 11. 部署建议

1. 先在能访问 Gamma / CLOB 的机器上跑 `python -m src.main --probe`，
   确认 7 个品种都能返回当前窗口。
2. paper 模式至少跑 3–7 天，重点看：
   - `status.json` 里每个品种是否在 0–60s 有报价；
   - `trades.jsonl` 的 `reason` 分布（tp / time_exit / settle）；
   - 净 PnL 与 gross PnL 的差值（手续费影响）；
   - 部分成交频率。
3. 实盘第一周把 `STAKE_USD` 降到 10U 以下（例如 2U）验证成交和费用，
   同时设置 `MAX_DAILY_LOSS_USD`。
4. 用 systemd / docker 托管时，确保进程有可写 `data/`；系统时区不影响
   计算，但**时钟必须准确（建议 NTP）**，因为 5m 窗口边界完全依赖 unix 时间。
5. 如果与 `02.poly1h` 共用钱包/API key：两者**共享 collateral**。本项目只下
   FAK、不挂 GTD，因此不会被 1h bot 的 startup reconcile 取消；但资金池是
   共享的，建议给两个策略分别预留资金，或先只让其中一个跑实盘。
6. Web 后端默认监听 `0.0.0.0:8033`。对公网暴露前必须设置 `WEB_PASSWORD`
   （HTTP Basic），或把 `WEB_HOST` 改为 `127.0.0.1` / 内网地址；否则同网络
   任何人都能改配置或触发全部平仓。

## 12. 测试矩阵

`python -m pytest -q`（43 个测试）：

- `test_config.py`：文档默认参数、非法阈值、费后为负识别；
- `test_strategy.py`：0–60s 入场、60s 后 TP、bid 触发、240s 强平、
  部分成交、结算、stale quote；
- `test_paper_broker.py`：ask/bid 阶梯、限价约束、WS 兜底；
- `test_market_ws.py`：BBO / REST midpoint / 过期报价的优先级；
- `test_market_service.py`：当前/下一窗口发现、边界推进、预发现；
- `test_state.py`：state 原子读写、trade JSONL；
- `test_runner.py`：AssetRunner 端到端“入场 → TP”、“入场 → 240s 强平”，
  以及重启后过期仓位的 Gamma 结算记账；
- `test_clob_parse.py`：FAK 买卖回报的 taking/making、base units 归一、
  fills 兜底解析；
- `test_runtime_config.py`：默认仅 BTC、启用/参数覆盖、持久化/重载、重置、非法值；
- `test_web.py`：页面、状态、配置 PUT、交易分页/筛选、统计、暂停/平仓、
  HTTP Basic 鉴权。

## 13. 后续可做（不在本次范围）

- 用真实服务器数据跑 7 币统一回测，校准文档参数在最近 6 个月的表现；
- 手续费敏感性 / 费率来源（CLOB `/fee-rate`）自动切换；
- maker 版本（GTD + 撤单 + 心跳）；
- 按方向轮动（文档 §5.2 指出 BTC 的 DOWN/UP 会按月轮动）；
- 动态 TP（>= breakeven + 最小净利）作为可选的研究模式；
- Web 看板（当前用 `status.json` + 日志）。
