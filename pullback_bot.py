#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
pullback_bot.py —— 版本A:顺大势、买回调(Alpaca 纸面交易)

规则(全部机械执行,没有任何主观判断):
  趋势过滤  收盘价 > 200日均线,才允许买
  买入信号  RSI(2) < 10,或 从10日最高收盘价回落 >= 2×ATR(14)
  仓位      股数 = 账户净值×1% ÷ (2×ATR)   → 止损被打掉时约亏账户的1%
  初始止损  信号日收盘价 − 2×ATR(下单时就挂在券商端,GTC)
  止盈一半  收盘价 >= 成本 + 2R 时,次日开盘卖一半,剩余止损上移到成本价以上
  移动止损  卖出一半后,止损 = max(原止损, 持仓期最高收盘 − 3×ATR),只上移不下移
  时间止损  持有满10个交易日还没涨到 +2R,次日开盘全部卖出

节奏:每个交易日收盘后运行一次。信号在T日收盘产生,T+1日开盘成交。

用法:
  python pullback_bot.py backtest          先回测,看历史表现
  python pullback_bot.py run --dry-run     只打印今天会做什么,不下单
  python pullback_bot.py run               真正向纸面账户下单
  python pullback_bot.py status            查看当前持仓与止损

密钥放在同目录 .env 文件里(两种写法都认):
  ALPACA_API_KEY=xxxx        或  APCA_API_KEY_ID=xxxx
  ALPACA_SECRET_KEY=xxxx     或  APCA_API_SECRET_KEY=xxxx
"""
import argparse
import json
import math
import os
import sys
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

import numpy as np
import pandas as pd

# ───────────────────────── 参数(想调就改这里) ─────────────────────────
UNIVERSE = ["SPY", "QQQ", "NVDA", "ORCL", "AMAT"]
RISK_PCT = 0.01        # 每笔交易的风险 = 账户净值的 1%
SMA_LEN = 200          # 趋势过滤均线
RSI_LEN = 2
RSI_BUY = 10           # RSI(2) 低于此值算"大跌"
ATR_LEN = 14
DROP_ATR = 2.0         # 或者:从10日高点回落 >= 2×ATR
DROP_LOOKBACK = 10
STOP_ATR = 2.0         # 初始止损距离
TP_R = 2.0             # 涨到 +2R 卖一半
TRAIL_ATR = 3.0        # 剩余仓位的移动止损距离
MAX_HOLD = 10          # 时间止损(交易日)
MAX_POSITIONS = 4      # 同时最多持有几只
MAX_POS_PCT = 0.25     # 单只市值上限 = 净值的 25%(ATR 很小时防止仓位过大)
SLIPPAGE = 0.0005      # 回测滑点 5 bps
DATA_FEED = "iex"      # 免费账户用 iex

HERE = Path(__file__).resolve().parent
NAME = "pullback"            # 状态/日志文件名前缀
EXIT_NAME = "时间止损"        # manage_position 返回 exit 时的出场原因名称
STATE_FILE = HERE / "pullback_state.json"
LOG_FILE = HERE / "pullback_log.txt"


# ───────────────────────── 指标与规则(回测和实盘共用) ─────────────────────────
def add_indicators(df: pd.DataFrame) -> pd.DataFrame:
    """df 需要 open/high/low/close 四列,按日期升序。"""
    d = df.copy()
    c = d["close"]
    d["sma200"] = c.rolling(SMA_LEN).mean()
    prev = c.shift(1)
    tr = pd.concat([d["high"] - d["low"], (d["high"] - prev).abs(), (d["low"] - prev).abs()], axis=1).max(axis=1)
    d["atr"] = tr.ewm(alpha=1 / ATR_LEN, adjust=False, min_periods=ATR_LEN).mean()
    chg = c.diff()
    up = chg.clip(lower=0).ewm(alpha=1 / RSI_LEN, adjust=False, min_periods=RSI_LEN).mean()
    dn = (-chg.clip(upper=0)).ewm(alpha=1 / RSI_LEN, adjust=False, min_periods=RSI_LEN).mean()
    d["rsi2"] = 100 - 100 / (1 + up / dn.replace(0, np.nan))
    d.loc[dn == 0, "rsi2"] = 100.0
    d["hi10"] = c.rolling(DROP_LOOKBACK).max()
    return d


def entry_signal(row) -> tuple[bool, str]:
    """返回 (是否买入, 白话理由)。"""
    if any(pd.isna(row[k]) for k in ("sma200", "atr", "rsi2", "hi10")):
        return False, "数据不足200天"
    if row["close"] <= row["sma200"]:
        return False, f"收盘 {row['close']:.2f} 在200日线 {row['sma200']:.2f} 下方 → 趋势向下,不接飞刀"
    drop = (row["hi10"] - row["close"]) / row["atr"]
    if row["rsi2"] < RSI_BUY:
        return True, f"上升趋势中急跌:RSI(2)={row['rsi2']:.1f} < {RSI_BUY}"
    if drop >= DROP_ATR:
        return True, f"上升趋势中急跌:从10日高点回落 {drop:.1f}×ATR"
    return False, f"趋势向上但没有大跌(RSI2={row['rsi2']:.1f},回落 {drop:.1f}×ATR)"


def position_size(equity: float, close: float, atr: float) -> tuple[int, float]:
    """按1%风险反推股数,返回 (股数, 止损价)。"""
    risk_per_share = STOP_ATR * atr
    qty = math.floor(equity * RISK_PCT / risk_per_share)
    qty = min(qty, math.floor(equity * MAX_POS_PCT / close))
    return max(qty, 0), round(close - risk_per_share, 2)


def manage_position(pos: dict, row) -> tuple[str, str]:
    """
    每个收盘后调用一次,更新 pos 并返回 (动作, 白话理由)。
    动作: 'hold' | 'half'(卖一半) | 'exit'(全部卖出)
    pos 字段: entry, stop, init_stop, qty, bars, high, half
    """
    close, atr = float(row["close"]), float(row["atr"])
    pos["bars"] += 1
    pos["high"] = max(pos["high"], close)
    r = pos["entry"] - pos["init_stop"]
    if not pos["half"]:
        target = pos["entry"] + TP_R * r
        if close >= target:
            pos["half"] = True
            pos["stop"] = round(max(pos["stop"], pos["entry"], pos["high"] - TRAIL_ATR * atr), 2)
            return "half", f"收盘 {close:.2f} 达到 +{TP_R:g}R 目标 {target:.2f} → 卖一半,剩余止损抬到 {pos['stop']:.2f}"
        if pos["bars"] >= MAX_HOLD:
            return "exit", f"持有 {pos['bars']} 天仍未到 +{TP_R:g}R → 时间止损,全部卖出"
        return "hold", f"持有第 {pos['bars']} 天,止损 {pos['stop']:.2f},目标 {target:.2f}"
    new_stop = round(max(pos["stop"], pos["high"] - TRAIL_ATR * atr), 2)
    moved = new_stop > pos["stop"]
    pos["stop"] = new_stop
    return "hold", f"跟踪剩余仓位,移动止损 {'上移到' if moved else '保持'} {new_stop:.2f}"


# ───────────────────────── Alpaca 连接 ─────────────────────────
def load_env():
    f = HERE / ".env"
    if f.exists():
        for line in f.read_text().splitlines():
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                k, v = line.split("=", 1)
                os.environ.setdefault(k.strip(), v.strip().strip('"').strip("'"))
    key = os.getenv("ALPACA_API_KEY") or os.getenv("APCA_API_KEY_ID")
    sec = os.getenv("ALPACA_SECRET_KEY") or os.getenv("APCA_API_SECRET_KEY")
    if not key or not sec:
        sys.exit("找不到 Alpaca 密钥:请在本文件同目录建 .env,写入 ALPACA_API_KEY 和 ALPACA_SECRET_KEY(纸面账户的密钥)")
    return key, sec


def fetch_bars(key, sec, symbols, days) -> dict[str, pd.DataFrame]:
    from alpaca.data.enums import Adjustment, DataFeed
    from alpaca.data.historical import StockHistoricalDataClient
    from alpaca.data.requests import StockBarsRequest
    from alpaca.data.timeframe import TimeFrame

    client = StockHistoricalDataClient(key, sec)
    req = StockBarsRequest(
        symbol_or_symbols=symbols,
        timeframe=TimeFrame.Day,
        start=datetime.now(timezone.utc) - timedelta(days=days),
        feed=DataFeed(DATA_FEED),
        adjustment=Adjustment.SPLIT,
    )
    raw = client.get_stock_bars(req).df
    out = {}
    for sym in symbols:
        if sym not in raw.index.get_level_values(0):
            print(f"  [警告] {sym} 没有取到数据,跳过")
            continue
        d = raw.xs(sym)[["open", "high", "low", "close"]].copy()
        d.index = pd.to_datetime(d.index).tz_convert("America/New_York").normalize().tz_localize(None)
        out[sym] = d[~d.index.duplicated(keep="last")].sort_index()
    return out


# ───────────────────────── 回测 ─────────────────────────
def backtest(bars: dict[str, pd.DataFrame], capital=100_000.0, verbose=True):
    """信号在T日收盘,T+1开盘成交,含滑点;止损盘中触发,跳空低开按开盘价成交。"""
    data = {s: add_indicators(d) for s, d in bars.items()}
    dates = sorted(set().union(*[d.index for d in data.values()]))
    cash, positions, pending, trades, curve = capital, {}, {}, [], []

    def close_out(sym, qty, price, day, why):
        nonlocal cash
        p = positions[sym]
        cash += qty * price
        p["pnl"] += qty * (price - p["entry"])
        p["qty"] -= qty
        if p["qty"] == 0:
            trades.append(dict(symbol=sym, entry_date=p["date"], exit_date=day, entry=p["entry"],
                               pnl=p["pnl"], r=p["pnl"] / p["risk"], pct_equity=p["pnl"] / p["eq0"] * 100, reason=why))
            del positions[sym]

    for day in dates:
        # 1) 开盘:执行昨天收盘后挂的单
        for sym, od in list(pending.items()):
            d = data[sym]
            if day not in d.index:
                continue
            del pending[sym]
            o = d.at[day, "open"]
            if od["type"] == "buy":
                px = o * (1 + SLIPPAGE)
                if od["qty"] * px > cash:
                    od["qty"] = math.floor(cash / px)
                if od["qty"] < 1:
                    continue
                cash -= od["qty"] * px
                positions[sym] = dict(entry=px, stop=od["stop"], init_stop=od["stop"], qty=od["qty"], bars=0,
                                      high=px, half=False, date=day, pnl=0.0,
                                      risk=od["qty"] * od["risk_ps"], eq0=od["eq0"])
            elif sym in positions:
                q = positions[sym]["qty"] if od["type"] == "exit" else positions[sym]["qty"] // 2
                if q:
                    close_out(sym, q, o * (1 - SLIPPAGE), day, EXIT_NAME if od["type"] == "exit" else "卖出一半")
        # 2) 盘中:止损
        for sym in list(positions):
            d = data[sym]
            if day in d.index and d.at[day, "low"] <= positions[sym]["stop"]:
                p = positions[sym]
                px = min(d.at[day, "open"], p["stop"]) * (1 - SLIPPAGE)
                close_out(sym, p["qty"], px, day, "移动止损" if p["half"] else "初始止损")
        # 3) 收盘:管理持仓 + 找新信号
        equity = cash + sum(p["qty"] * data[s]["close"].asof(day) for s, p in positions.items())
        for sym, p in positions.items():
            d = data[sym]
            if day in d.index and p["date"] != day:
                act, _ = manage_position(p, d.loc[day])
                if act != "hold":
                    pending[sym] = dict(type=act)
        for sym, d in data.items():
            if day not in d.index or sym in positions or sym in pending:
                continue
            if len(positions) + sum(1 for o in pending.values() if o["type"] == "buy") >= MAX_POSITIONS:
                break
            row = d.loc[day]
            ok, _ = entry_signal(row)
            if ok:
                qty, stop = position_size(equity, row["close"], row["atr"])
                if qty >= 1:
                    pending[sym] = dict(type="buy", qty=qty, stop=stop, eq0=equity, risk_ps=row["close"] - stop)
        curve.append((day, equity))

    eq = pd.Series(dict(curve))
    t = pd.DataFrame(trades)
    if verbose:
        print(f"\n回测区间 {eq.index[0].date()} → {eq.index[-1].date()}  初始资金 ${capital:,.0f}")
        print(f"期末净值 ${eq.iloc[-1]:,.0f}  总收益 {(eq.iloc[-1] / capital - 1) * 100:+.1f}%  "
              f"最大回撤 {((eq / eq.cummax()) - 1).min() * 100:.1f}%")
        if t.empty:
            print("没有产生任何交易(多半是全程在200日线下方,被趋势过滤挡住了)")
        else:
            win = t["pnl"] > 0
            streak = longest = 0
            for w in win:
                streak = 0 if w else streak + 1
                longest = max(longest, streak)
            print(f"交易 {len(t)} 笔  胜率 {win.mean() * 100:.0f}%  平均盈利 {t.loc[win, 'r'].mean():+.2f}R  "
                  f"平均亏损 {t.loc[~win, 'r'].mean():+.2f}R  每笔期望 {t['r'].mean():+.2f}R")
            print(f"最大单笔盈利 {t['pct_equity'].max():+.2f}% 账户  最大单笔亏损 {t['pct_equity'].min():+.2f}% 账户  最长连亏 {longest} 笔")
            print("\n按出场原因:")
            print(t.groupby("reason")["r"].agg(笔数="count", 平均R="mean").round(2).to_string())
            print("\n按股票:")
            print(t.groupby("symbol")["r"].agg(笔数="count", 平均R="mean", 合计R="sum").round(2).to_string())
            out = HERE / f"{NAME}_backtest_trades.csv"
            t.to_csv(out, index=False)
            print(f"\n逐笔明细已存到 {out.name}")
    return eq, t


# ───────────────────────── 纸面交易(每日收盘后运行) ─────────────────────────
DRY = False


def log(msg):
    print(msg)
    if DRY:                                     # 试运行不写日志文件
        return
    with open(LOG_FILE, "a", encoding="utf-8") as f:
        f.write(f"{datetime.now():%Y-%m-%d %H:%M} {msg}\n")


def load_state() -> dict:
    return json.loads(STATE_FILE.read_text()) if STATE_FILE.exists() else {}


def save_state(state):
    STATE_FILE.write_text(json.dumps(state, indent=2, ensure_ascii=False))


class Broker:
    def __init__(self, key, sec, dry):
        from alpaca.trading.client import TradingClient
        self.c = TradingClient(key, sec, paper=True)   # 写死 paper=True:本程序只碰纸面账户
        self.dry = dry

    def equity(self):
        return float(self.c.get_account().equity)

    def market_open(self):
        return self.c.get_clock().is_open

    def positions(self):
        return {p.symbol: p for p in self.c.get_all_positions()}

    def open_orders(self, sym):
        from alpaca.trading.enums import QueryOrderStatus
        from alpaca.trading.requests import GetOrdersRequest
        return self.c.get_orders(GetOrdersRequest(status=QueryOrderStatus.OPEN, symbols=[sym]))

    def cancel_all(self, sym):
        if self.dry:
            return
        for o in self.open_orders(sym):
            self.c.cancel_order_by_id(o.id)
        for _ in range(20):                      # 等撤单生效,否则股份还被旧单占着
            if not self.open_orders(sym):
                return
            time.sleep(0.5)
        raise RuntimeError(f"{sym} 撤单超时,请到 Alpaca 网页检查")

    def buy_with_stop(self, sym, qty, stop):
        from alpaca.trading.enums import OrderClass, OrderSide, TimeInForce
        from alpaca.trading.requests import MarketOrderRequest, StopLossRequest
        if self.dry:
            return
        self.c.submit_order(MarketOrderRequest(
            symbol=sym, qty=qty, side=OrderSide.BUY, time_in_force=TimeInForce.GTC,
            order_class=OrderClass.OTO, stop_loss=StopLossRequest(stop_price=stop)))

    def sell_market(self, sym, qty):
        from alpaca.trading.enums import OrderSide, TimeInForce
        from alpaca.trading.requests import MarketOrderRequest
        if not self.dry and qty > 0:
            self.c.submit_order(MarketOrderRequest(symbol=sym, qty=qty, side=OrderSide.SELL, time_in_force=TimeInForce.DAY))

    def stop_sell(self, sym, qty, stop):
        from alpaca.trading.enums import OrderSide, TimeInForce
        from alpaca.trading.requests import StopOrderRequest
        if not self.dry and qty > 0:
            self.c.submit_order(StopOrderRequest(symbol=sym, qty=qty, side=OrderSide.SELL,
                                                 time_in_force=TimeInForce.GTC, stop_price=stop))


def run(dry: bool):
    global DRY
    DRY = dry
    key, sec = load_env()
    br = Broker(key, sec, dry)
    bars = fetch_bars(key, sec, UNIVERSE, days=450)
    if br.market_open():                        # 盘中运行时,今天的K线还没走完,不用它
        today = pd.Timestamp.now(tz="America/New_York").normalize().tz_localize(None)
        bars = {s: d[d.index < today] for s, d in bars.items()}
        print("[提示] 现在是盘中,今天的K线未完成,按昨天收盘计算。建议收盘后再运行。")
    data = {s: add_indicators(d) for s, d in bars.items()}
    state, held, equity = load_state(), br.positions(), br.equity()
    log(f"===== {'[试运行] ' if dry else ''}账户净值 ${equity:,.2f} =====")

    # 1) 对账 + 管理已有持仓
    for sym in list(state):
        pos, d = state[sym], data.get(sym)
        if sym not in held:
            if pos.get("pending") and br.open_orders(sym):
                log(f"{sym}: 买单还在排队,等待成交")
                continue
            log(f"{sym}: 账户里已无持仓 → {'买单未成交,或成交当天就被止损' if pos.get('pending') else '止损已触发'},清除记录")
            del state[sym]
            continue
        if d is None:
            continue
        last, day = d.iloc[-1], str(d.index[-1].date())
        qty = int(float(held[sym].qty))
        if pos.pop("pending", False):           # 昨晚的买单今天成交了:用真实成交价记账
            fill = float(held[sym].avg_entry_price)
            pos.update(entry=fill, high=fill, qty=qty, bars=0, half=False, date=day)
            log(f"{sym}: 已成交 {qty} 股 @ {fill:.2f},止损 {pos['stop']:.2f}(风险约 ${(fill - pos['stop']) * qty:,.0f})")
            continue
        if pos.get("last_day") == day:          # 今天已经处理过,重复运行不重复计数
            log(f"{sym}: 今天已处理,跳过")
            continue
        pos["last_day"], pos["qty"] = day, qty
        old_stop = pos["stop"]
        act, why = manage_position(pos, last)
        log(f"{sym}: {why}")
        if act == "exit":
            br.cancel_all(sym)
            br.sell_market(sym, qty)
            log(f"{sym}: → 明日开盘卖出全部 {qty} 股")
        elif act == "half":
            half = qty // 2
            br.cancel_all(sym)
            br.sell_market(sym, half)
            br.stop_sell(sym, qty - half, pos["stop"])
            log(f"{sym}: → 明日开盘卖出 {half} 股,其余 {qty - half} 股止损挂在 {pos['stop']:.2f}")
        elif pos["stop"] != old_stop:
            br.cancel_all(sym)
            br.stop_sell(sym, qty, pos["stop"])

    # 2) 找新的买点
    for sym, d in data.items():
        if sym in state:
            continue
        if sym in held:
            log(f"{sym}: 账户里有不属于本策略的持仓,不动它")
            continue
        last = d.iloc[-1]
        ok, why = entry_signal(last)
        if not ok:
            log(f"{sym}: 不买 —— {why}")
            continue
        if len(state) >= MAX_POSITIONS:
            log(f"{sym}: 有信号但已满 {MAX_POSITIONS} 只持仓,放弃")
            continue
        qty, stop = position_size(equity, float(last["close"]), float(last["atr"]))
        if qty < 1:
            log(f"{sym}: 有信号但按1%风险算不足1股,放弃")
            continue
        try:
            br.buy_with_stop(sym, qty, stop)
        except Exception as e:                  # 例如购买力不足被拒:记下来,继续处理别的股票
            log(f"{sym}: 有信号但下单被拒 —— {e}")
            continue
        state[sym] = dict(pending=True, stop=stop, init_stop=stop, signal_close=float(last["close"]))
        log(f"{sym}: 买入 —— {why}")
        log(f"{sym}: → 明日开盘买 {qty} 股(约 ${qty * last['close']:,.0f}),止损 {stop:.2f},"
            f"打到止损约亏 ${(last['close'] - stop) * qty:,.0f} = 净值的 {(last['close'] - stop) * qty / equity * 100:.2f}%")

    if dry:
        print("\n[试运行] 以上只是演示,没有下单,也没有保存状态。")
    else:
        save_state(state)


def status():
    key, sec = load_env()
    br = Broker(key, sec, dry=True)
    state, held = load_state(), br.positions()
    print(f"纸面账户净值 ${br.equity():,.2f}")
    if not state:
        print("本策略当前空仓。")
    for sym, pos in state.items():
        if pos.get("pending"):
            print(f"{sym}: 买单排队中,止损 {pos['stop']:.2f}")
        elif sym in held:
            h = held[sym]
            print(f"{sym}: {h.qty} 股 成本 {pos['entry']:.2f} 现价 {float(h.current_price):.2f} "
                  f"浮盈 ${float(h.unrealized_pl):+,.0f} 止损 {pos['stop']:.2f} 持有 {pos['bars']} 天 "
                  f"{'(已卖一半,跟踪中)' if pos['half'] else ''}")


def main(desc="版本A:顺大势买回调(Alpaca 纸面交易)"):
    ap = argparse.ArgumentParser(description=desc)
    sub = ap.add_subparsers(dest="cmd", required=True)
    r = sub.add_parser("run", help="每日收盘后运行")
    r.add_argument("--dry-run", action="store_true", help="只打印,不下单")
    b = sub.add_parser("backtest", help="历史回测")
    b.add_argument("--years", type=float, default=3)
    sub.add_parser("status", help="查看持仓")
    a = ap.parse_args()
    if a.cmd == "run":
        run(a.dry_run)
    elif a.cmd == "status":
        status()
    else:
        k, s = load_env()
        backtest(fetch_bars(k, s, UNIVERSE, days=int(a.years * 365) + 300))


if __name__ == "__main__":
    main()
