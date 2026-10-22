"""前瞻性模拟投资流程（第五阶段新增，任务书第九章）。

完整闭环：
1. 获取当时可用的真实数据
2. 生成智谱独立研究报告
3. 生成反方审查
4. 导出GPT-6研究交接包
5. 等待GPT-6正式交易建议
6. 等待用户确认模拟委托
7. 按明确规则判断模拟成交
8. 更新模拟账户
9. 记录未来复盘结果

所有步骤均留痕，不可静默修改历史。
模拟账户与真实账本物理隔离。
"""
import datetime as dt
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from stockpilot import REPORTS, audit, iso, load_json, save_json, setup_logger
from stockpilot import ledger as L
from stockpilot import simulation as sim
from stockpilot import timeutil as tu
from stockpilot.collectors import quote as q
from stockpilot.collectors import news as news_col
from stockpilot.research import build_research, build_adversarial, fetch_fundamentals
from stockpilot import crosscheck, tradeability, handoff_v4

log = setup_logger("forward_sim", "forward_sim.log")

WATCH = [("600519", "gssh0600519"), ("000001", "gssz0000001"), ("600036", "gssh0600036")]


def run_daily_simulation(trade_date=None, auto_confirm=False):
    """运行一天的完整模拟投资流程。

    Args:
        trade_date: 交易日期（YYYY-MM-DD），默认当天
        auto_confirm: 是否自动确认委托（测试用，实际应由用户确认）

    Returns:
        包含整个流程结果的字典
    """
    trade_date = trade_date or tu.today().isoformat()
    run_id = f"sim_{trade_date}_{tu.now().strftime('%H%M%S')}"

    result = {
        "run_id": run_id,
        "trade_date": trade_date,
        "generated_at": iso(),
        "steps": {},
    }

    # ===== 步骤1：获取当时可用的真实数据 =====
    log.info(f"[{run_id}] 步骤1：获取行情数据")
    snap = q.collect_market_snapshot()
    result["steps"]["market_snapshot"] = {
        "status": "ok" if snap else "failed",
        "trade_date": snap.get("trade_date") if snap else None,
        "indices_count": len(snap.get("indices", [])) if snap else 0,
    }

    # ===== 步骤2：获取新闻与公告 =====
    log.info(f"[{run_id}] 步骤2：获取新闻与公告")
    news = news_col.collect_news(WATCH, 14)
    result["steps"]["news"] = {
        "status": "ok" if news else "failed",
        "events_count": len(news.get("events", [])) if news else 0,
    }

    # ===== 步骤3：数据质量检查 =====
    log.info(f"[{run_id}] 步骤3：数据质量检查")
    from stockpilot import quality
    cross_codes = [("sh600519", "600519", "1.600519"), ("sz000001", "000001", "0.000001")]
    qual = quality.run_quality_checks(snap, load_json("reports/market_list.json"), cross_codes)
    result["steps"]["quality"] = {
        "status": "ok" if qual else "failed",
        "overall": qual.get("overall") if qual else None,
    }

    # ===== 步骤4：股票筛选 =====
    log.info(f"[{run_id}] 步骤4：股票筛选")
    from stockpilot import screener
    cands_doc = screener.run_screening(load_json("reports/market_list.json")["items"])
    result["steps"]["screening"] = {
        "status": "ok" if cands_doc else "failed",
        "candidates_count": len(cands_doc.get("candidates", [])) if cands_doc else 0,
    }

    # ===== 步骤5：智谱独立研究 =====
    log.info(f"[{run_id}] 步骤5：智谱独立研究")
    from stockpilot.research import build_research
    research = build_research(
        snap, load_json("reports/market_list.json"),
        cands_doc.get("candidates", []), news, qual,
        load_json("reports/fundamentals.json") if os.path.exists("reports/fundamentals.json") else None,
        load_json("reports/cross_verify.json") if os.path.exists("reports/cross_verify.json") else None,
        load_json("reports/tradeability.json") if os.path.exists("reports/tradeability.json") else None,
    )
    result["steps"]["research"] = {
        "status": "ok" if research else "failed",
        "candidates_count": len(research.get("candidates", [])) if research else 0,
    }

    # ===== 步骤6：反方审查 =====
    log.info(f"[{run_id}] 步骤6：反方审查")
    from stockpilot.research import build_adversarial
    adv = build_adversarial(
        cands_doc.get("candidates", []), load_json("reports/market_list.json"),
        snap, news, qual,
        load_json("reports/fundamentals.json") if os.path.exists("reports/fundamentals.json") else None,
        load_json("reports/cross_verify.json") if os.path.exists("reports/cross_verify.json") else None,
        load_json("reports/tradeability.json") if os.path.exists("reports/tradeability.json") else None,
    )
    result["steps"]["adversarial"] = {
        "status": "ok" if adv else "failed",
        "summary": adv.get("summary", {}) if adv else None,
    }

    # ===== 步骤7：生成交接包 =====
    log.info(f"[{run_id}] 步骤7：生成GPT-6交接包")
    from stockpilot import handoff_v4
    ho = handoff_v4.export_handoff_v4(
        snap, load_json("reports/market_list.json"), news,
        cands_doc, research, adv, qual, L.portfolio()
    )
    result["steps"]["handoff"] = {
        "handoff_id": ho.get("handoff_id"),
        "complete": ho.get("manifest", {}).get("integrity", {}).get("all_files_present", False),
        "zip": ho.get("zip"),
    }

    # ===== 步骤8：模拟交易（如果有候选且用户确认） =====
    log.info(f"[{run_id}] 步骤8：模拟交易决策")
    sim_result = run_simulated_trade(
        cands_doc.get("candidates", []), research, adv, auto_confirm=auto_confirm
    )
    result["steps"]["simulated_trade"] = sim_result

    # ===== 步骤9：预测留痕 =====
    log.info(f"[{run_id}] 步骤9：预测留痕")
    pred_id = L.add_prediction(
        subject=f"市场环境@{trade_date}",
        claim=json.dumps({
            "top_candidates": [c.get("code") for c in cands_doc.get("candidates", [])][:5],
            "adversarial_summary": adv.get("summary", {}) if adv else {},
        }, ensure_ascii=False),
        horizon="下一交易日",
    )
    result["steps"]["prediction"] = {"id": pred_id, "subject": f"市场环境@{trade_date}"}

    # 保存完整结果
    save_json(results, REPORTS + f"/simulation_daily_{trade_date}.json")
    audit("daily_simulation_complete", run_id=run_id, trade_date=trade_date)

    return result


def run_simulated_trade(candidates, research, adv, auto_confirm=False):
    """执行模拟交易流程。

    从候选中选择最佳标的，模拟完整的买入流程。
    """
    if not candidates:
        return {"status": "no_candidates", "note": "无可交易候选"}

    # 选择评分最高的候选
    best = max(candidates, key=lambda c: c.get("score", 0))
    code = best.get("code")
    name = best.get("name")
    price = (best.get("trend", {}) or {}).get("close") or best.get("price")

    if not price or price <= 0:
        return {"status": "no_price", "code": code, "note": "无有效价格"}

    # 检查可交易性
    from stockpilot import tradeability as ta
    td = ta.tradable_decision(
        {"code": code, "name": name, "price": price, "market": "A",
         "amount": best.get("amount", 0), "pct_chg": best.get("trend", {}).get("chg_10d_pct")},
        cash=1000.0,
    )
    if not td.get("tradable"):
        return {
            "status": "not_tradable",
            "code": code,
            "reasons": td.get("blocking_reasons", []),
        }

    # 创建模拟建议
    aid = sim.sim_advice(
        source="GPT-6(模拟)",
        action="buy",
        code=code,
        name=name,
        price=price,
        shares=100,
        reason=f"基于研究报告：{best.get('score_reasons', [])[:2]}",
        handoff_id=None,
    )

    # 模拟用户确认
    if not auto_confirm:
        return {
            "status": "pending_user_confirm",
            "advice_id": aid,
            "code": code,
            "name": name,
            "price": price,
            "shares": 100,
            "note": "等待用户确认模拟委托",
        }

    # 用户已确认，创建委托
    oid = sim.sim_order(aid, "buy", code, name, price=price, shares=100)

    # 执行成交
    result = sim.process_order(oid)

    return {
        "status": result.get("status"),
        "code": code,
        "name": name,
        "price": price,
        "shares": 100,
        "fill_price": result.get("fill_price"),
        "fee": result.get("fee"),
        "rule": result.get("rule"),
        "advice_id": aid,
        "order_id": oid,
    }


if __name__ == "__main__":
    import sys
    trade_date = sys.argv[1] if len(sys.argv) > 1 else None
    auto_confirm = "--auto-confirm" in sys.argv
    result = run_daily_simulation(trade_date=trade_date, auto_confirm=auto_confirm)
    print(json.dumps(result, ensure_ascii=False, indent=2))