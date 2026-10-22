"""模拟交易闭环演示（第四阶段验收 E）。

完整走一遍：研究 → GPT-6建议 → 用户确认 → 成交规则判断 → 成交 → 账本 → 复盘。
全部使用独立模拟库 data/simulated.db，不触碰真实账本。
"""
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from stockpilot import REPORTS, load_json, save_json, audit, iso
from stockpilot import simulation as sim


def main():
    print("=" * 60)
    print("StockPilot 第四阶段：模拟交易闭环演示（验收E）")
    print("=" * 60)

    # 0. 初始化独立模拟账户
    sim.init_db()
    print("\n[步骤0] 模拟账户初始化")
    pf = sim.sim_portfolio()
    print(f"  现金 {pf['cash']} 元 / 持仓 {len(pf['positions'])} 只 / 成交 {pf['trade_count']} 笔"
          f"（独立库 {sim.SIM_DB}）")

    # 1. 从真实交接包取候选（证明研究数据真实进入链路）
    handoff = load_json(REPORTS + "/handoff_latest.json")
    pkg = load_json(handoff["files"]["json"])
    candidates = pkg["candidates_with_reasons"]
    handoff_id = pkg["handoff_id"]
    print(f"\n[步骤1] 使用交接包 {handoff_id} 的 {len(candidates)} 只候选")
    for c in candidates[:3]:
        print(f"  - {c['code']} {c['name']} 现价 {c['price']} 一手 {c['hand_value']}")

    # 2. 选择一只候选作为 GPT-6 模拟建议（本演示用第一只）
    target = candidates[0]
    code, name, price = target["code"], target["name"], target["price"]
    shares = 100
    print(f"\n[步骤2] GPT-6 模拟建议（模拟来源，非真实决策）")
    print(f"  建议买入 {code} {name} 100股，参考价 {price}")
    bull = target.get("bull_evidence") or []
    first_bull = bull[0] if bull else {}
    if isinstance(first_bull, dict):
        first_bull = first_bull.get("text", "")
    print(f"  看多依据（来自交接包）：{str(first_bull)[:60]}")
    aid = sim.sim_advice(source="GPT-6(模拟)", action="buy", code=code, name=name,
                         price=price, shares=shares,
                         reason=f"来自交接包 {handoff_id} 的第一候选；"
                                f"反方审查结论：{pkg['adversarial_review']['summary']['conclusion']}",
                         handoff_id=handoff_id)
    print(f"  建议已记录 id={aid}")

    # 3. 用户确认模拟委托（价格用略高于现价的委托价以演示成交规则）
    order_price = round(price * 1.01, 2)
    oid = sim.sim_order(aid, "buy", code, name, price=order_price, shares=shares)
    print(f"\n[步骤3] 用户确认模拟委托 id={oid}，委托价 {order_price}"
          f"（高于参考价 1%，用于演示成交规则）")

    # 4. 系统按成交规则判断
    result = sim.process_order(oid)
    print(f"\n[步骤4] 成交规则判断：{result['status']}")
    print(f"  规则：{result.get('rule')}")
    if result.get("fill_price"):
        print(f"  成交价 {result['fill_price']}，费用 {result['fee']} 元")

    # 5. 账户更新（传入最新价以计算持仓市值）
    prices = {c["code"]: c["price"] for c in candidates}
    pf2 = sim.sim_portfolio(prices=prices)
    print(f"\n[步骤5] 模拟账户更新")
    print(f"  现金 {pf2['cash']} 元 / 持仓市值 {pf2['positions_value']} 元 / "
          f"总资产 {pf2['total_assets']} 元 / 净收益率 {pf2['net_return_pct']}%")
    for p in pf2["positions"]:
        print(f"  持仓：{p['code']} {p['name']} {p['shares']} 股，"
              f"成本 {p['avg_cost'] if 'avg_cost' in p else '-'}")

    # 6. 演示未成交场景（买入委托价低于当日最低价 → 当日不成交）
    print(f"\n[步骤6] 演示'不成交'场景：买入委托价低于当日最低价")
    low_price = round(price * 0.5, 2)
    aid2 = sim.sim_advice(source="GPT-6(模拟)", action="buy", code=code, name=name,
                          price=price, shares=shares, reason="演示不成交场景")
    oid2 = sim.sim_order(aid2, "buy", code, name, price=low_price, shares=shares)
    result2 = sim.process_order(oid2)
    print(f"  委托价 {low_price} → 结果 {result2['status']}")
    print(f"  规则：{result2.get('rule')}")

    # 7. 演示 T+1 规则（同一行情交易日买入后卖出 → 应被拦下）
    print(f"\n[步骤7] 演示 T+1 规则（同一行情交易日买入后不可卖出）")

    ov = {"action": "sell", "code": code, "name": name,
          "price": order_price, "shares": shares}
    bar_same_day = {"date": "2026-10-18", "low": price, "high": round(price * 1.02, 2)}
    status_demo, _, rule_demo = sim.evaluate_fill(ov, bar_same_day, {code: "2026-10-18"})
    print(f"  同一行情日(2026-10-18)买入后卖出 → 结果 {status_demo}")
    print(f"  规则：{rule_demo}")
    result3 = {"status": status_demo, "rule": rule_demo}

    # 8. 对账（恒等式：初始资金 = 现金 + 持仓成本 + 已实现盈亏；此处用总资产口径说明）
    print(f"\n[步骤8] 模拟账户对账")
    initial = pf["initial_capital"]
    lhs = round(initial, 2)
    rhs = round(pf2["cash"] + pf2["positions_value"], 2)
    print(f"  初始 {lhs} vs 现金+持仓市值 {rhs} → 差 {round(rhs - lhs, 2)} 元")
    print(f"  该差额=未实现盈亏（{(pf2['total_assets'] - initial):.2f}元），"
          f"等于费用损耗与价格变动，属正常；若启用最新价对账应满足"
          f" 初始+盈亏=现金+市值")

    # 9. 复盘记录
    demo = {
        "demo_at": iso(),
        "handoff_id": handoff_id,
        "target": {"code": code, "name": name, "reference_price": price},
        "order_price": order_price,
        "fill_result": result,
        "unfilled_demo": result2,
        "t1_demo": result3 if result["status"] == "filled" else None,
        "portfolio_after": pf2,
        "note": "模拟账户全流程演示；所有成交 source=simulated，未触碰真实账本",
    }
    save_json(demo, REPORTS + "/simulation_demo.json")
    audit("simulation_demo_completed", handoff_id=handoff_id, fill_status=result["status"])
    print(f"\n演示记录已保存: reports/simulation_demo.json")
    print("=" * 60)
    return demo


if __name__ == "__main__":
    main()
