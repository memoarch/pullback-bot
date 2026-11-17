#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
alphatrend_bot.py —— 版本C:AlphaTrend 趋势跟随(Alpaca 纸面交易)

AlphaTrend 是 KivancOzbilgic 在 TradingView 上的指标,这里按原公式移植(日线):
  ATR        = 真实波幅的14日简单平均
  上轨 upT   = 最低价 − 1×ATR        下轨 downT = 最高价 + 1×ATR
  RSI(14) ≥ 50(动能偏多):AlphaTrend = max(upT, 昨天的线)   → 只升不降
  RSI(14) < 50(动能偏空):AlphaTrend = min(downT, 昨天的线) → 只降不升
  (原指标默认用 MFI,需要成交量;免费 IEX 数据的成交量只是全市场的一小部分,
    所以用原指标自带的"无成交量 → 改用 RSI"选项)

规则(机械执行):
  买入信号  AlphaTrend 线上穿两天前的自己(转多),次日开盘买入
  仓位      股数 = 账户净值×1% ÷ (2×ATR)   → 止损被打掉时约亏账户的1%
  初始止损  信号日收盘价 − 2×ATR(随买单一起挂在券商端,GTC,此后不动)
  卖出信号  AlphaTrend 线下穿两天前的自己(转空)→ 次日开盘全部卖出
  没有止盈、没有时间止损、没有趋势均线过滤:只看这一条线

与版本A/B 共用同一个纸面账户、同一套下单/回测代码,各自记账
(alphatrend_state.json / alphatrend_log.txt)。同一只股票同一时间只归一个策略。

用法与版本A相同:
  python alphatrend_bot.py backtest | run --dry-run | run | status
"""
import numpy as np
import pandas as pd

import pullback_bot as core

AT_LEN = 14            # AlphaTrend 的周期(ATR 与 RSI 共用),原指标默认 14
AT_COEFF = 1.0         # ATR 倍数,原指标默认 1

_base_indicators = core.add_indicators


def add_indicators(df: pd.DataFrame) -> pd.DataFrame:
    d = _base_indicators(df)                    # 保留引擎的 atr(Wilder 14),仓位和止损用它
    c = d["close"]
    prev = c.shift(1)
    tr = pd.concat([d["high"] - d["low"], (d["high"] - prev).abs(), (d["low"] - prev).abs()], axis=1).max(axis=1)
    at_atr = tr.rolling(AT_LEN).mean()          # 原指标用简单平均
    chg = c.diff()
    up = chg.clip(lower=0).ewm(alpha=1 / AT_LEN, adjust=False, min_periods=AT_LEN).mean()
    dn = (-chg.clip(upper=0)).ewm(alpha=1 / AT_LEN, adjust=False, min_periods=AT_LEN).mean()
    rsi = 100 - 100 / (1 + up / dn.replace(0, np.nan))
    rsi[dn == 0] = 100.0
    d["at_rsi"] = rsi

    up_t = (d["low"] - AT_COEFF * at_atr).to_numpy()
    dn_t = (d["high"] + AT_COEFF * at_atr).to_numpy()
    bull = (rsi >= 50).to_numpy()
    r = rsi.to_numpy()
    at = np.full(len(d), np.nan)
    for i in range(len(d)):
        if np.isnan(up_t[i]) or np.isnan(r[i]):
            continue
        if np.isnan(at[i - 1]) if i else True:  # 第一根有效K线:直接取当天的轨道
            at[i] = up_t[i] if bull[i] else dn_t[i]
        elif bull[i]:
            at[i] = max(up_t[i], at[i - 1])
        else:
            at[i] = min(dn_t[i], at[i - 1])
    d["at"] = at
    d["at2"] = d["at"].shift(2)                 # 两天前的线
    d["at_up"] = (d["at"] > d["at2"]) & (d["at"].shift(1) <= d["at2"].shift(1))   # 今天上穿
    d["at_dn"] = (d["at"] < d["at2"]) & (d["at"].shift(1) >= d["at2"].shift(1))   # 今天下穿
    # 方向:最近一次穿越是上穿记 +1,是下穿记 −1(漏跑一天也不会错过卖出)
    d["at_dir"] = pd.Series(np.where(d["at_up"], 1.0, np.where(d["at_dn"], -1.0, np.nan)), index=d.index).ffill()
    # 原指标只在"转多"的第一次上穿标 BUY;多头途中线再抬高一级不算新信号
    d["at_buy"] = d["at_up"] & (d["at_dir"].shift(1) != 1)
    return d


def entry_signal(row) -> tuple[bool, str]:
    if any(pd.isna(row[k]) for k in ("at", "at2", "atr", "at_dir")):
        return False, "数据不足"
    if row["at_buy"]:
        return True, f"AlphaTrend 转多:线 {row['at']:.2f} 上穿两天前的 {row['at2']:.2f}(RSI14={row['at_rsi']:.0f})"
    if row["at_dir"] > 0:
        return False, f"AlphaTrend 多头已在途中(线 {row['at']:.2f}),今天不是转多当天,不追"
    return False, f"AlphaTrend 空头(线 {row['at']:.2f},RSI14={row['at_rsi']:.0f}),等它转多"


def manage_position(pos: dict, row) -> tuple[str, str]:
    close = float(row["close"])
    pos["bars"] += 1
    pos["high"] = max(pos["high"], close)
    gain = (close / pos["entry"] - 1) * 100
    if row["at_dir"] < 0:
        return "exit", f"AlphaTrend 转空(线 {row['at']:.2f}) → 趋势结束,全部卖出(持有 {pos['bars']} 天,{gain:+.1f}%)"
    return "hold", f"持有第 {pos['bars']} 天({gain:+.1f}%),AlphaTrend 线 {row['at']:.2f} 仍为多头,转空或止损 {pos['stop']:.2f} 才卖"


# 把版本C的规则装进共用引擎
core.NAME = "alphatrend"
core.EXIT_NAME = "AlphaTrend转空"
core.STATE_FILE = core.HERE / "alphatrend_state.json"
core.LOG_FILE = core.HERE / "alphatrend_log.txt"
core.add_indicators = add_indicators
core.entry_signal = entry_signal
core.manage_position = manage_position

if __name__ == "__main__":
    core.main("版本C:AlphaTrend 趋势跟随(Alpaca 纸面交易)")
