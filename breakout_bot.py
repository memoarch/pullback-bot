#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
breakout_bot.py —— 版本B:突破跟踪(Alpaca 纸面交易)

规则(机械执行):
  买入信号  收盘价 > 此前20个交易日的最高价(创20日新高),次日开盘买入
  仓位      股数 = 账户净值×1% ÷ (2×ATR)   → 止损被打掉时约亏账户的1%
  初始止损  信号日收盘价 − 2×ATR(随买单一起挂在券商端,GTC,此后不动)
  卖出信号  收盘价 < 此前10个交易日的最低价 → 次日开盘全部卖出
  没有止盈、没有时间止损:亏的砍得快,赚的一直拿到趋势掉头

预期形态:胜率偏低,靠少数大行情赚钱。

与版本A(pullback_bot.py)共用同一个纸面账户、同一套下单/回测代码,
但各自记账(breakout_state.json / breakout_log.txt)。同一只股票同一时间
只归一个策略:谁先买到归谁,另一个策略会跳过。

用法与版本A相同:
  python breakout_bot.py backtest | run --dry-run | run | status
"""
import pandas as pd

import pullback_bot as core

ENTRY_LEN = 20         # 突破此前 N 日最高价买入
EXIT_LEN = 10          # 跌破此前 N 日最低价卖出

_base_indicators = core.add_indicators


def add_indicators(df: pd.DataFrame) -> pd.DataFrame:
    d = _base_indicators(df)
    d["hi_n"] = d["high"].rolling(ENTRY_LEN).max().shift(1)   # 不含当天
    d["lo_n"] = d["low"].rolling(EXIT_LEN).min().shift(1)
    return d


def entry_signal(row) -> tuple[bool, str]:
    if pd.isna(row["hi_n"]) or pd.isna(row["atr"]):
        return False, "数据不足"
    if row["close"] > row["hi_n"]:
        return True, f"收盘 {row['close']:.2f} 突破{ENTRY_LEN}日高点 {row['hi_n']:.2f}"
    return False, f"收盘 {row['close']:.2f} 未突破{ENTRY_LEN}日高点 {row['hi_n']:.2f}"


def manage_position(pos: dict, row) -> tuple[str, str]:
    close = float(row["close"])
    pos["bars"] += 1
    pos["high"] = max(pos["high"], close)
    gain = (close / pos["entry"] - 1) * 100
    if close < row["lo_n"]:
        return "exit", f"收盘 {close:.2f} 跌破{EXIT_LEN}日低点 {row['lo_n']:.2f} → 趋势结束,全部卖出(持有 {pos['bars']} 天,{gain:+.1f}%)"
    return "hold", f"持有第 {pos['bars']} 天({gain:+.1f}%),跌破 {row['lo_n']:.2f} 或止损 {pos['stop']:.2f} 才卖"


# 把版本B的规则装进共用引擎
core.NAME = "breakout"
core.EXIT_NAME = f"跌破{EXIT_LEN}日低点"
core.STATE_FILE = core.HERE / "breakout_state.json"
core.LOG_FILE = core.HERE / "breakout_log.txt"
core.add_indicators = add_indicators
core.entry_signal = entry_signal
core.manage_position = manage_position

if __name__ == "__main__":
    core.main("版本B:突破跟踪(Alpaca 纸面交易)")
