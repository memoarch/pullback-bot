# pullback-bot

版本A:顺大势、买回调。Alpaca **纸面账户**上的机械化短线策略,每个交易日收盘后运行一次。

## 规则

| 环节 | 规则 |
|---|---|
| 趋势过滤 | 收盘价 > 200日均线才允许买 |
| 买入 | RSI(2) < 10,或从10日最高收盘价回落 ≥ 2×ATR(14);次日开盘成交 |
| 仓位 | 股数 = 净值×1% ÷ (2×ATR),单只市值 ≤ 净值25%,最多4只 |
| 初始止损 | 信号日收盘 − 2×ATR,随买单一起挂在券商端(GTC) |
| 止盈一半 | 收盘 ≥ 成本 + 2R → 次日开盘卖一半,止损抬到成本以上 |
| 移动止损 | 剩余仓位:持仓期最高收盘 − 3×ATR,只上移 |
| 时间止损 | 满10个交易日未到 +2R → 次日开盘全部卖出 |

股票池:SPY、QQQ、NVDA、ORCL、AMAT(在 `pullback_bot.py` 顶部修改)。

## 用法

```bash
pip install -r requirements.txt
# 同目录建 .env(不会进仓库):
#   ALPACA_API_KEY=...
#   ALPACA_SECRET_KEY=...
python pullback_bot.py backtest         # 历史回测
python pullback_bot.py run --dry-run    # 只打印,不下单
python pullback_bot.py run              # 向纸面账户下单
python pullback_bot.py status           # 查看持仓
```

## 文件

- `pullback_state.json` 当前持仓状态(每日运行后更新并提交)
- `pullback_log.txt` 每日决策日志,每只股票一句白话理由

## 基准回测(2023-01 → 2026-11-16,IEX日线,初始 $100k)

总收益 +42.0%,最大回撤 −12.7%,153 笔,胜率 52%,每笔期望 +0.31R,最长连亏 10 笔。
同期五只等权买入持有 +235%(最大回撤 −31%)。规则固定后不随回测结果调参,以纸面交易记录为准。
