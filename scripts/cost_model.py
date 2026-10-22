"""1000 元账户交易成本实测计算（用于可行性报告第 7 章）。
A 股现行规则：佣金万分之2.5（最低5元）、印花税卖出千分之一、过户费沪深均万0.1、ETF免印花税。
"""
import json
import os

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
OUT = os.path.join(BASE, "evidence", "cost_model.json")

CASES = [
    # (标的类型, 单笔价格, 股数)
    ("ETF 510300 @4.236", 4.236, 200),   # 847.2 元
    ("低价股 @5 元", 5.0, 200),          # 1000 元
    ("低价股 @10 元", 10.0, 100),        # 1000 元
    ("中价股 @20 元", 20.0, 50),         # 1000 元
]


def cost(buy_amount, sell_amount, is_etf):
    """按现行规则估算双边成本（万元2.5佣金含最低5元、卖出印花税0.1%非ETF、过户费万0.1双向）"""
    fee = {}

    def commission(turnover):
        c = turnover * 0.00025
        return max(c, 5.0)

    c_in = commission(buy_amount)
    c_out = commission(sell_amount)
    stamp = 0.0 if is_etf else sell_amount * 0.001
    transfer = buy_amount * 0.00001 + sell_amount * 0.00001
    total = c_in + c_out + stamp + transfer
    return {
        "buy_commission": round(c_in, 2),
        "sell_commission": round(c_out, 2),
        "stamp_duty": round(stamp, 2),
        "transfer_fee": round(transfer, 2),
        "round_trip_total": round(total, 2),
        "round_trip_pct_of_1000": round(total / 1000 * 100, 3),
    }


def main():
    out = {"assumptions": {
        "佣金费率": "万分之2.5（每笔最低5元）",
        "印花税": "卖出千分之一（ETF免征）",
        "过户费": "沪深均万分之0.1（双向）",
        "最小交易单位": "100股（1手）",
        "初始资金": "1000元",
    }, "cases": []}
    for name, price, shares in CASES:
        is_etf = "ETF" in name
        buy_amount = round(price * shares, 2)
        # 假设不涨不跌卖出
        sell_amount = buy_amount
        c = cost(buy_amount, sell_amount, is_etf)
        c.update({
            "case": name, "shares": shares, "price": price,
            "position_value": buy_amount,
            "cash_left": round(1000 - buy_amount - c["round_trip_total"], 2),
            "breakeven_move_pct": round(c["round_trip_total"] / buy_amount * 100, 3),
        })
        out["cases"].append(c)
    out["conclusion"] = (
        "1000元账户单笔双边成本约10.0~11.0元（最低佣金5元×2 占主导，ETF免印花税省1元），"
        "相当于本金的1.00%~1.10%。标的需上涨约1.10%~1.18%才回本；"
        "若每月交易2次，年化摩擦成本约24%~26%，会严重侵蚀收益。"
        "高频策略在1000元账户上不可行；ETF因免印花税成本最低。"
    )
    with open(OUT, "w", encoding="utf-8") as f:
        json.dump(out, f, ensure_ascii=False, indent=2)
    print(json.dumps(out, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
