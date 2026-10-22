"""回测框架对比小规模实测：vectorbt vs backtrader，用茅台真实数据跑 20 日均线策略。
输出：安装状态、依赖、执行时间、指标（收益/回撤/交易次数/费用），落盘 evidence/backtest_probes.jsonl
"""
import json
import os
import time
import traceback
import datetime as dt

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
OUT = os.path.join(BASE, "evidence", "backtest_probes.jsonl")


def rec(name, **kw):
    kw["probe"] = name
    kw["at"] = dt.datetime.now().isoformat(timespec="seconds")
    with open(OUT, "a", encoding="utf-8") as f:
        f.write(json.dumps(kw, ensure_ascii=False, default=str) + "\n")
    print(f"--- {name}: {json.dumps({k: v for k, v in kw.items() if k not in ('probe', 'at')}, ensure_ascii=False, default=str)[:400]}")


def fetch_history(code="sh600519", name="600519"):
    """用腾讯源取前复权日线"""
    import requests
    r = requests.get("https://web.ifzq.gtimg.cn/appstock/app/fqkline/get",
                     params={"param": f"{code},day,2024-01-01,2026-10-17,800,qfq"}, timeout=30)
    j = r.json()
    rows = j["data"][code]["qfqday"]
    return [{"date": x[0], "open": float(x[1]), "close": float(x[2]),
             "high": float(x[3]), "low": float(x[4]), "volume": float(x[5])} for x in rows]


def vbt_test(bars):
    import pandas as pd
    import vectorbt as vbt
    df = pd.DataFrame(bars).set_index("date")
    close = df["close"]
    ma = close.rolling(20).mean()
    entries = (close > ma).astype(bool)
    exits = (close < ma).astype(bool)
    t0 = time.time()
    # size_type='value' 表示"投入多少钱"，更贴近 A 股 1000 元账户
    pf = vbt.Portfolio.from_signals(
        close, entries=entries, exits=exits,
        fees=0.0003, freq="1D",
        init_cash=1000.0,           # 1000 元账户
        direction="longonly",
        size_type="value", size=900,  # 每次最多投入 900 元（留手续费与零头）
    )
    stats = {
        "elapsed_s": round(time.time() - t0, 2),
        "total_return_pct": round((float(pf.final_value()) / 1000.0 - 1) * 100, 2),
        "benchmark_return_pct": round((float(close.iloc[-1]) / float(close.iloc[0]) - 1) * 100, 2),
        "max_dd_pct": round(float(pf.max_drawdown()) * 100, 2),
        "trades": int(pf.trades.count()),
        "win_rate_pct": round(float(pf.trades.win_rate() or 0) * 100, 2),
        "fees_paid": round(float(pf.orders.fees.sum()), 2),
        "end_value": round(float(pf.final_value()), 2),
    }
    rec("vectorbt 20日均线 (value sizing)", bars=len(bars), **stats)


def bt_test(bars):
    import backtrader as bt
    import pandas as pd

    class SmaCross(bt.Strategy):
        params = (("fast", 20),)

        def __init__(self):
            self.ma = bt.ind.SMA(period=self.p.fast)
            self.crossover = bt.ind.CrossOver(self.data.close, self.ma)
            self.n_trades = 0

        def next(self):
            if not self.position:
                if self.crossover > 0:
                    # A股整手买入，留出佣金与零头
                    size = int(self.broker.getcash() // (self.data.close[0] * 100)) * 100
                    if size > 0:
                        self.buy(size=size)
                        self.n_trades += 1
            elif self.crossover < 0:
                self.sell(size=self.position.size)
                self.n_trades += 1

    df = pd.DataFrame(bars)
    df.index = pd.to_datetime(df["date"])
    # backtrader 1.9 的 pandas3 兼容性问题：改用双列表喂数据
    data = bt.feeds.PandasData(dataname=df[["open", "high", "low", "close", "volume"]])
    cerebro = bt.Cerebro()
    cerebro.addstrategy(SmaCross)
    cerebro.adddata(data)
    cerebro.broker.setcash(1000.0)
    cerebro.broker.setcommission(commission=0.0003)
    cerebro.addanalyzer(bt.analyzers.TradeAnalyzer, _name="ta")
    cerebro.addanalyzer(bt.analyzers.DrawDown, _name="dd")
    t0 = time.time()
    res = cerebro.run()
    st = res[0]
    val = cerebro.broker.getvalue()
    ta = st.analyzers.ta.get_analysis()
    dd = st.analyzers.dd.get_analysis()
    rec("backtrader 20日均线 (整手)", bars=len(bars),
        elapsed_s=round(time.time() - t0, 2), end_value=round(val, 2),
        trades_closed=ta.get("total", {}).get("closed"),
        trades_won=ta.get("won", {}).get("total"),
        trades_lost=ta.get("lost", {}).get("total"),
        max_dd_pct=round(dd.get("max", {}).get("drawdown", 0), 2))


def main():
    # A. 茅台：1000 元买不起一手（一手≈15万元）→ 记录"不可交易"事实
    bars_m = fetch_history("sh600519", "600519")
    rec("fetch.tencent.qfqday sh600519", bars=len(bars_m), first=bars_m[0]["date"],
        last=bars_m[-1]["date"], last_close=bars_m[-1]["close"],
        note="一手(100股)价格约 %.0f 元，1000 元账户无法买入整手" % (bars_m[-1]["close"] * 100))
    # B. ETF 510300：一手≈400 元，可交易
    bars_e = fetch_history("sh510300", "510300")
    rec("fetch.tencent.qfqday sh510300", bars=len(bars_e), first=bars_e[0]["date"],
        last=bars_e[-1]["date"], last_close=bars_e[-1]["close"],
        note="一手(100股)价格约 %.0f 元，1000 元账户可买入 2 手" % (bars_e[-1]["close"] * 100))
    for name, fn in (("vectorbt", vbt_test), ("backtrader", bt_test)):
        try:
            fn(bars_e)
        except Exception as e:
            rec(f"{name} ERROR", error=f"{type(e).__name__}: {e}", trace=traceback.format_exc()[-800:])


if __name__ == "__main__":
    main()
