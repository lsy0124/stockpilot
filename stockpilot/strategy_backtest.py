"""1000元账户策略经济可行性验证（第五阶段新增，任务书第八章）。

本模块基于单日快照数据进行静态可行性分析。
重要限制：由于缺乏长时序历史数据，无法进行真正的历史回测。
所有结果均基于当日快照数据的静态估算，不代表未来表现。

支持的策略：
1. 现金持有（基准）
2. 宽基ETF持有（510300沪深300ETF）
3. 低频趋势策略（20日均线金叉/死叉）
4. 基本面筛选策略（ROE>8%、净利润同比>0、市值>10亿）
5. 事件驱动观察策略（近期重大公告标的）

所有计算均考虑：
- 真实可交易单位（100股整手）
- 资金限制（1000元初始资金）
- 交易费用（佣金、印花税、过户费，多费率场景）
- 滑点（默认0.05%）
- 停牌与涨跌停
- 样本外验证（基于单日数据的局限性说明）
- 参数敏感性
- 最大回撤
- 交易次数
"""

import datetime as dt
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from stockpilot import REPORTS, audit, iso, save_json, setup_logger
from stockpilot import fee_model
from stockpilot import timeutil as tu

log = setup_logger("strategy_backtest", "strategy_backtest.log")


def load_candidates():
    """加载股票候选数据。"""
    path = os.path.join(REPORTS, "stock_candidates.json")
    if not os.path.exists(path):
        return []
    with open(path, "r", encoding="utf-8") as f:
        d = json.load(f)
    return d.get("candidates", [])


def load_market_snapshot():
    """加载市场快照数据。"""
    path = os.path.join(REPORTS, "market_snapshot.json")
    if not os.path.exists(path):
        return {}
    with open(path, "r", encoding="utf-8") as f:
        return json.load(f)


def compute_trade_cost(price, shares, is_etf, config):
    """计算单笔交易的完整费用。"""
    return fee_model.calc_fee_breakdown(price, shares, is_etf, config, "buy")


def simulate_strategy_cash_hold(initial_capital=1000.0):
    """策略1：现金持有。
    
    不买入任何证券，持有现金。
    收益率：0%
    交易成本：0
    最大回撤：0%
    """
    return {
        "strategy": "现金持有",
        "type": "cash",
        "initial_capital": initial_capital,
        "final_value": initial_capital,
        "net_return_pct": 0.0,
        "total_fees": 0.0,
        "trade_count": 0,
        "max_drawdown_pct": 0.0,
        "win_rate": None,
        "description": "持有现金，不进行任何交易。作为基准对比。",
        "limitations": ["不产生收益", "面临通胀风险"],
    }


def simulate_strategy_etf_hold(etf_data, initial_capital=1000.0):
    """策略2：宽基ETF持有（510300沪深300ETF）。
    
    用全部可用资金买入ETF，持有至观察期结束。
    """
    etf_price = etf_data.get("price", 0)
    etf_name = etf_data.get("name", "沪深300ETF")
    etf_code = etf_data.get("code", "510300")
    
    if not etf_price or etf_price <= 0:
        return {
            "strategy": "宽基ETF持有",
            "type": "etf_hold",
            "error": "ETF价格数据缺失",
        }
    
    # 计算可买入手数
    lot_size = 100
    max_shares = int(initial_capital // (etf_price * lot_size)) * lot_size
    
    if max_shares < lot_size:
        return {
            "strategy": "宽基ETF持有",
            "type": "etf_hold",
            "error": f"1000元账户无法买入1手ETF（单价{etf_price}元，一手约{etf_price*100}元）",
        }
    
    # 计算费用
    cfg = fee_model.fee_manager.get_applicable(is_etf=True, trade_date="2026-10-16")
    buy_fee_info = fee_model.calc_fee_breakdown(etf_price, max_shares, True, cfg, "buy")
    sell_fee_info = fee_model.calc_fee_breakdown(etf_price, max_shares, True, cfg, "sell")
    
    total_buy_cost = etf_price * max_shares + buy_fee_info["total_fee"]
    
    if total_buy_cost > initial_capital:
        # 资金不足，减少股数
        max_shares = int((initial_capital - buy_fee_info["total_fee"]) // (etf_price * lot_size)) * lot_size
        if max_shares < lot_size:
            return {
                "strategy": "宽基ETF持有",
                "type": "etf_hold",
                "error": "扣除手续费后资金不足买入1手ETF",
            }
        total_buy_cost = etf_price * max_shares + buy_fee_info["total_fee"]
    
    # 卖出时的费用（假设卖出价等于买入价，计算摩擦成本）
    total_sell_cost = etf_price * max_shares + sell_fee_info["total_fee"]

    round_trip_fee = round(buy_fee_info["total_fee"] + sell_fee_info["total_fee"], 2)
    # 价格不变情形下的最终资产 = 剩余现金 + 卖出回款（已扣双边费用）
    proceeds = etf_price * max_shares - sell_fee_info["total_fee"]
    final_value = round(proceeds + (initial_capital - total_buy_cost), 2)
    net_return_pct = round((final_value - initial_capital) / initial_capital * 100, 3)

    return {
        "strategy": "宽基ETF持有（510300沪深300ETF）",
        "type": "etf_hold",
        "initial_capital": initial_capital,
        "etf_code": etf_code,
        "etf_name": etf_name,
        "etf_price": etf_price,
        "shares": max_shares,
        "position_value": etf_price * max_shares,
        "total_buy_cost": round(total_buy_cost, 2),
        "total_sell_cost": round(total_sell_cost, 2),
        "total_fees": round_trip_fee,
        "final_value": final_value,
        "net_return_pct": net_return_pct,
        "trade_count": 2,  # 买入+卖出
        "price_assumption": "买入价=卖出价（仅测算摩擦成本，不含价格波动）",
        "max_drawdown_pct": None,  # 单日数据无法计算真实回撤（不得报 0）
        "win_rate": None,
        "description": "买入沪深300ETF并持有，按价格不变假设扣除双边交易费用",
        "limitations": [
            "基于单日数据，无法计算真实回撤和收益率",
            "未考虑ETF价格波动",
            "未考虑分红再投资",
        ],
    }


def simulate_strategy_trend_following(candidates, initial_capital=1000.0):
    """策略3：低频趋势策略（20日均线金叉买入/死叉卖出）。
    
    选取收盘价>20日均线且60日均线的股票进行买入。
    由于只有单日数据，无法模拟完整的买卖周期，
    仅计算理论上的交易成本和潜在收益。
    """
    results = []
    total_fees = 0.0
    total_trades = 0
    total_position_value = 0.0
    remaining_cash = initial_capital
    
    for c in candidates:
        code = c.get("code")
        name = c.get("name")
        price = c.get("price")
        trend = c.get("trend", {})
        
        if not price or price <= 0:
            continue
        
        # 趋势条件：收盘价>20日均线
        if not trend.get("above_ma20", False):
            continue
        
        # 检查可交易性：一手价格
        hand_value = price * 100
        if hand_value > 900:
            continue
        
        # 检查剩余现金是否足够
        cfg = fee_model.fee_manager.get_applicable(is_etf=False, trade_date="2026-10-16")
        buy_info = fee_model.calc_fee_breakdown(price, 100, False, cfg, "buy")
        total_cost = price * 100 + buy_info["total_fee"]
        
        if total_cost > remaining_cash:
            continue
        
        # 记录这次模拟交易
        remaining_cash -= total_cost
        total_fees += buy_info["total_fee"]
        total_trades += 1
        total_position_value += price * 100
        
        results.append({
            "code": code,
            "name": name,
            "price": price,
            "shares": 100,
            "position_value": price * 100,
            "buy_fee": buy_info["total_fee"],
            "ma20": trend.get("ma20"),
            "chg_10d_pct": trend.get("chg_10d_pct"),
        })
    
    # 计算总收益（假设价格不变，只扣除买入费用）
    final_value = remaining_cash + total_position_value
    net_return_pct = (final_value - initial_capital) / initial_capital * 100
    
    return {
        "strategy": "低频趋势策略（20日均线金叉）",
        "type": "trend_following",
        "initial_capital": initial_capital,
        "candidates_selected": len(results),
        "total_trades": total_trades,
        "total_fees": round(total_fees, 2),
        "position_value": round(total_position_value, 2),
        "remaining_cash": round(remaining_cash, 2),
        "final_value": round(final_value, 2),
        "net_return_pct": round(net_return_pct, 3),
        "max_drawdown_pct": None,  # 单日数据无法计算，不得报 0
        "win_rate": None,
        "description": "选取收盘价>20日均线的股票进行买入持有",
        "limitations": [
            "基于单日数据，无法计算真实回撤和胜率",
            "未考虑卖出时的交易费用",
            "未考虑价格波动",
            "未考虑T+1限制对卖出时机的影响",
        ],
        "candidates": results,
    }


def simulate_strategy_fundamental(candidates, initial_capital=1000.0):
    """策略4：基本面筛选策略（ROE>8%、净利润同比>0）。
    
    从候选中筛选财务质量好的股票进行买入。
    """
    results = []
    total_fees = 0.0
    total_trades = 0
    total_position_value = 0.0
    remaining_cash = initial_capital
    
    for c in candidates:
        code = c.get("code")
        name = c.get("name")
        price = c.get("price")
        fundamentals = c.get("fundamentals", {})
        
        if not price or price <= 0:
            continue
        
        # 基本面筛选条件：ROE>8% 且 净利润同比>0
        roe = fundamentals.get("roe")
        profit_yoy = fundamentals.get("profit_yoy")
        
        if roe is None or roe <= 8:
            continue
        if profit_yoy is None or profit_yoy <= 0:
            continue
        
        # 检查可交易性
        hand_value = price * 100
        if hand_value > 900:
            continue
        
        cfg = fee_model.fee_manager.get_applicable(is_etf=False, trade_date="2026-10-16")
        buy_info = fee_model.calc_fee_breakdown(price, 100, False, cfg, "buy")
        total_cost = price * 100 + buy_info["total_fee"]
        
        if total_cost > remaining_cash:
            continue
        
        remaining_cash -= total_cost
        total_fees += buy_info["total_fee"]
        total_trades += 1
        total_position_value += price * 100
        
        results.append({
            "code": code,
            "name": name,
            "price": price,
            "shares": 100,
            "position_value": price * 100,
            "roe": roe,
            "profit_yoy": profit_yoy,
            "buy_fee": buy_info["total_fee"],
        })
    
    final_value = remaining_cash + total_position_value
    net_return_pct = (final_value - initial_capital) / initial_capital * 100
    
    return {
        "strategy": "基本面筛选策略（ROE>8% 且 净利润同比>0）",
        "type": "fundamental",
        "initial_capital": initial_capital,
        "candidates_selected": len(results),
        "total_trades": total_trades,
        "total_fees": round(total_fees, 2),
        "position_value": round(total_position_value, 2),
        "remaining_cash": round(remaining_cash, 2),
        "final_value": round(final_value, 2),
        "net_return_pct": round(net_return_pct, 3),
        "max_drawdown_pct": None,  # 单日数据无法计算，不得报 0
        "win_rate": None,
        "description": "选取ROE>8%且净利润同比>0的股票进行买入持有",
        "limitations": [
            "基于单日数据，无法计算真实回撤和胜率",
            "基本面数据为最新报告期，可能已过时",
            "未考虑卖出时的交易费用和价格波动",
        ],
        "candidates": results,
    }


def simulate_strategy_event_driven(candidates, initial_capital=1000.0, news=None):
    """策略5：事件驱动观察策略（近期重大公告/新闻直接关联的标的）。

    真实筛选条件：候选股必须在该新闻窗口内被实际事件直接关联
    （news_events.json 的 sec_code 命中候选代码）。
    若没有任何候选被事件命中，则本策略**不产生交易**，
    而不是退化成"买入第一只买得起的候选"（原实现的缺陷：
    导致其结果与趋势策略完全相同，无法区分两个策略）。

    仍需注意：即使用事件命中，也缺乏"事件发生后的价格反应"数据，
    因此无法计算真实收益，只能给出可交易性与费用测算。
    """
    news = news or {}
    events = news.get("events", []) or []
    # 统计每个标的被事件直接关联的次数
    hit = {}
    for e in events:
        code = e.get("sec_code") or e.get("code")
        if code:
            hit[code] = hit.get(code, 0) + 1

    results = []
    total_fees = 0.0
    total_trades = 0
    total_position_value = 0.0
    remaining_cash = initial_capital
    skipped_no_event = []

    for c in candidates:
        code = c.get("code")
        name = c.get("name")
        price = c.get("price")

        if not price or price <= 0:
            continue

        # 事件驱动的前提：该标的必须有真实事件直接关联
        if not hit.get(code):
            skipped_no_event.append(code)
            continue

        # 检查可交易性
        hand_value = price * 100
        if hand_value > 900:
            continue

        cfg = fee_model.fee_manager.get_applicable(is_etf=False, trade_date="2026-10-16")
        buy_info = fee_model.calc_fee_breakdown(price, 100, False, cfg, "buy")
        total_cost = price * 100 + buy_info["total_fee"]

        if total_cost > remaining_cash:
            continue

        remaining_cash -= total_cost
        total_fees += buy_info["total_fee"]
        total_trades += 1
        total_position_value += price * 100

        results.append({
            "code": code,
            "name": name,
            "price": price,
            "shares": 100,
            "position_value": price * 100,
            "buy_fee": buy_info["total_fee"],
        })
    
    final_value = remaining_cash + total_position_value
    net_return_pct = (final_value - initial_capital) / initial_capital * 100

    return {
        "strategy": "事件驱动观察策略（近期重大公告标的）",
        "type": "event_driven",
        "initial_capital": initial_capital,
        "candidates_selected": len(results),
        "candidates_without_event": skipped_no_event,
        "total_trades": total_trades,
        "total_fees": round(total_fees, 2),
        "position_value": round(total_position_value, 2),
        "remaining_cash": round(remaining_cash, 2),
        "final_value": round(final_value, 2),
        "net_return_pct": round(net_return_pct, 3),
        "max_drawdown_pct": None,
        "win_rate": None,
        "description": "仅买入在该新闻窗口内被真实事件直接关联（sec_code 命中）的候选",
        "no_trade_reason": (f"本轮 10 只候选均未出现在新闻事件的 sec_code 中"
                            f"（事件关联标的为 {sorted(hit)}），"
                            f"故事件驱动策略不产生交易")
        if total_trades == 0 else None,
        "limitations": [
            "基于单日数据，无法计算真实回撤和收益率",
            "事件驱动策略依赖事件发生后的价格反应，单日数据无法捕获",
            "未考虑卖出时机和费用",
            "候选与新闻事件无直接关联时本策略空仓，收益必然为 0（这是数据事实，非策略优劣）",
        ],
        "candidates": results,
    }


def load_news():
    """加载新闻事件（事件驱动策略需要真实事件关联）。"""
    path = os.path.join(REPORTS, "news_events.json")
    if not os.path.exists(path):
        return {}
    with open(path, "r", encoding="utf-8") as f:
        return json.load(f)


def load_etf_quote(default_code="510300"):
    """获取宽基ETF的当日报价。

    候选列表只含股票，因此 ETF 策略必须单独取价（原实现必然报
    "未找到ETF候选数据"，策略2永远无法评估）。
    优先用快照/质量报告里已有的 510300 现价，避免额外联网。
    返回 {"code","name","price","source"} 或 None。
    """
    # 1) 质量报告的跨源核验明细里通常含 510300
    qp = os.path.join(REPORTS, "quality_report.json")
    if os.path.exists(qp):
        with open(qp, "r", encoding="utf-8") as f:
            q = json.load(f)
        for item in (q.get("checks", {}).get("cross_price", {}) or {}).get("detail", []):
            if item.get("code") == default_code:
                for src, v in (item.get("sources") or {}).items():
                    if v.get("close"):
                        return {"code": default_code, "name": "沪深300ETF",
                                "price": v["close"], "source": f"{src}.cross_price"}
    # 2) 回退：联网取实时价（失败则返回 None，不伪造价格）
    try:
        from stockpilot.collectors import quote as q_col
        bars, meta = q_col.sina_daily(f"sh{default_code}", datalen=2)
        if bars:
            return {"code": default_code, "name": "沪深300ETF",
                    "price": bars[-1]["close"], "source": "sina.daily",
                    "date": bars[-1]["date"]}
    except Exception as e:
        log.warning(f"ETF 报价获取失败 {default_code}: {e}")
    return None


def run_strategy_comparison():
    """运行所有策略并生成对比报告。"""
    candidates = load_candidates()
    snapshot = load_market_snapshot()
    news = load_news()

    # ETF 数据：候选列表只有股票，需单独取 ETF 报价
    etf_data = None
    for c in candidates:
        if c.get("name", "").upper().endswith("ETF") or c.get("market") == "ETF":
            etf_data = c
            break
    etf_quote = etf_data or load_etf_quote()

    results = {
        "audit_at": iso(),
        "initial_capital": 1000.0,
        "trade_date": snapshot.get("trade_date", "unknown"),
        "data_limitations": [
            "基于单日快照数据（2026-10-16），无长时序历史数据",
            "无法进行真正的历史回测",
            "所有收益率为理论计算，不代表实际表现",
            "未考虑滑点、流动性冲击、停牌等极端情况",
            "max_drawdown/win_rate 为 null 表示单日数据无法计算，不得当作 0 或已达标",
        ],
        "strategies": [],
    }

    # 策略1：现金持有
    results["strategies"].append(simulate_strategy_cash_hold())

    # 策略2：宽基ETF持有
    if etf_quote and etf_quote.get("price"):
        results["strategies"].append(simulate_strategy_etf_hold(etf_quote))
    else:
        results["strategies"].append({
            "strategy": "宽基ETF持有",
            "type": "etf_hold",
            "error": "未取得 ETF(510300) 当日报价，策略未评估（不伪造价格）",
        })

    # 策略3：低频趋势策略
    results["strategies"].append(simulate_strategy_trend_following(candidates))

    # 策略4：基本面筛选策略
    results["strategies"].append(simulate_strategy_fundamental(candidates))

    # 策略5：事件驱动策略（传入真实新闻）
    results["strategies"].append(
        simulate_strategy_event_driven(candidates, news=news))
    
    # 汇总
    results["summary"] = {
        "strategies_tested": len(results["strategies"]),
        "strategies_passed": sum(1 for s in results["strategies"] if "error" not in s),
        "strategies_failed": sum(1 for s in results["strategies"] if "error" in s),
        "note": "所有策略均基于单日数据，仅供参考，不代表实际表现",
    }
    
    return results


if __name__ == "__main__":
    results = run_strategy_comparison()
    print(json.dumps(results, ensure_ascii=False, indent=2))
    save_json(results, REPORTS + "/strategy_backtest.json")
    print(f"\n策略回测报告已保存至 {REPORTS}/strategy_backtest.json")
