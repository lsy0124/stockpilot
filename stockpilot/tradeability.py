"""1000 元账户可交易性筛选器（第五阶段重写，任务书第六条）。

要求：
- 最小买入单位、实际价格、可用现金、预估佣金、适用税费
- 交易所与品种限制、停牌与涨跌停状态、账户权限、买入后剩余现金
- 券商真实费率未确认 → 可配置假设 + 多费率敏感性测试
- 成本过高的股票不得排在候选前列而不说明原因
- 允许现金持有成为正式策略选项

分层筛选（任务书第七条）：
  L1 数据完整性与交易权限（科创板/北交所/ST/停牌迹象）
  L2 1000 元可交易性（一手价格 + 费用 + 剩余现金）
  L3 流动性与重大风险（成交额/异常波动）
  L4 基本面/估值/趋势/行业（趋势来自日线；财务来自业绩报表）
  L5 新闻催化与反方证据
"""
import datetime as dt

from stockpilot import REPORTS, audit, iso, save_json, setup_logger
from stockpilot import ledger as L
from stockpilot import timeutil as tu
from stockpilot import fee_model

log = setup_logger("tradeability", "tradeability.log")

LOT_SIZE = 100
ACCOUNT_CASH = 1000.0
MAX_HAND_VALUE = 900.0           # 一手价格上限（留佣金与零头）
MIN_AMOUNT = 50_000_000          # 日成交额下限 5000 万

# 全局费率管理器（假设配置，非真实券商费率）
fee_manager = fee_model.FeeModelManager()


def fee_scenarios_for(price, shares=100, is_etf=False,
                      trade_date=None):
    """多费率敏感性：返回每种适用配置下的双边成本与回本涨幅。

    trade_date: 交易日期（YYYY-MM-DD），用于匹配生效日期的费率配置。
    若未提供，使用当前交易日期。
    """
    if trade_date is None:
        trade_date = tu.today().isoformat()
    out = {}
    for config in fee_manager.list_configs():
        if config.is_etf != is_etf:
            continue
        if not config.is_applicable(trade_date):
            continue
        rt = fee_model.calc_round_trip(price, shares, is_etf, config)
        out[config.config_id] = {
            "label": config.name,
            "source": config.source,
            "round_trip_total": rt["round_trip_total"],
            "pct_of_capital": rt["pct_of_initial_capital"],
            "breakeven_pct": rt["breakeven_pct"],
        }
    return out


def tradable_decision(item, cash=ACCOUNT_CASH):
    """对单个标的做完整可交易性判定，返回分层结果"""
    d = {"code": item.get("code"), "name": item.get("name"),
         "price": item.get("price"), "checks": {}, "tradable": False,
         "blocking_reasons": [], "notes": []}
    price = item.get("price")
    code = d["code"] or ""
    name = (d["name"] or "")
    market = item.get("market")
    is_etf = name.upper().endswith("ETF") or market == "ETF"

    # ---- L1 数据完整性与权限 ----
    l1 = {}
    if not price or price <= 0:
        d["blocking_reasons"].append("无有效价格（可能停牌或数据缺失）")
        l1["price_valid"] = False
    else:
        l1["price_valid"] = True
    l1["st_risk"] = name.upper().startswith(("ST", "*ST")) or "退" in name
    if l1["st_risk"]:
        d["blocking_reasons"].append("ST/退市风险标的")
    l1["star_market"] = code.startswith("688")
    if l1["star_market"]:
        d["blocking_reasons"].append("科创板需50万资产+2年经验权限")
    l1["bj_market"] = market == "BJ"
    if l1["bj_market"]:
        d["blocking_reasons"].append("北交所需单独开通权限")
    l1["limit_status"] = "normal"
    pct = item.get("pct_chg")
    if pct is not None:
        if pct >= 9.9:
            l1["limit_status"] = "limit_up"
            d["notes"].append(f"当日涨幅 {pct}%，可能已涨停（次日买入可能无法成交）")
        elif pct <= -9.9:
            l1["limit_status"] = "limit_down"
            d["notes"].append(f"当日跌幅 {pct}%，可能已跌停")
    l1["halt_suspect"] = (item.get("amount") in (0, None)) and l1["price_valid"]
    if l1["halt_suspect"]:
        d["notes"].append("成交额为0，疑似停牌")
    d["checks"]["L1_permission_data"] = l1

    # ---- L2 可交易性 ----
    l2 = {}
    trade_date = item.get("trade_date") or tu.today().isoformat()
    if price and price > 0:
        hand = round(price * LOT_SIZE, 2)
        l2["hand_value"] = hand
        l2["max_hand_value"] = MAX_HAND_VALUE
        l2["cash"] = cash
        if hand > MAX_HAND_VALUE:
            d["blocking_reasons"].append(f"一手价格 {hand} 元超过 {MAX_HAND_VALUE} 元上限")
        # 多费率敏感性（用最小可买手数）
        shares = max(LOT_SIZE, int(cash // (price * LOT_SIZE)) * LOT_SIZE)
        l2["planned_shares"] = shares
        l2["fee_scenarios"] = fee_scenarios_for(price, shares, is_etf, trade_date)
        worst = (l2["fee_scenarios"].get("stock_pessimistic_5yuan") or
                 l2["fee_scenarios"].get("etf_pessimistic_5yuan") or
                 next(iter(l2["fee_scenarios"].values())) if l2["fee_scenarios"] else None)
        # 现金是否足够（含最悲观费用）
        if worst:
            need = price * shares + worst["round_trip_total"] / 2  # 买入侧费用
            l2["cash_required"] = round(need, 2)
            l2["cash_left_after"] = round(cash - need, 2)
            if need > cash:
                d["blocking_reasons"].append(
                    f"买入 {shares} 股需 {need:.2f} 元（含费用），超过可用现金 {cash}")
            else:
                l2["cash_sufficient"] = True
    d["checks"]["L2_tradeability"] = l2

    # ---- L3 流动性 ----
    l3 = {}
    amount = item.get("amount") or 0
    l3["amount"] = amount
    l3["min_amount"] = MIN_AMOUNT
    if amount < MIN_AMOUNT:
        d["blocking_reasons"].append(f"日成交额 {amount/1e4:.0f} 万元低于 5000 万门槛")
    else:
        l3["liquidity_ok"] = True
    d["checks"]["L3_liquidity"] = l3

    d["tradable"] = len(d["blocking_reasons"]) == 0
    if d["tradable"]:
        # 成本提示：即使可交易，也要说明成本水平
        worst = (d["checks"].get("L2_tradeability") or {}).get("fee_scenarios", {}).get(
            "pessimistic_5yuan", {})
        if worst.get("breakeven_pct", 0) > 1.5:
            d["notes"].append(
                f"成本提示：悲观费率下回本需涨 {worst['breakeven_pct']}%，"
                f"高于1.5%阈值，不应仅因技术形态靠前而忽视")
    return d


def screen_tradable(market_items, cash=ACCOUNT_CASH):
    """全市场可交易性筛选（L1-L3 为硬门槛，L4/L5 在 research 层叠加）"""
    out = {"screened_at": iso(), "account": {"cash": cash, "lot_size": LOT_SIZE,
                                            "max_hand_value": MAX_HAND_VALUE,
                                            "fee_model": "assumption"},
           "tradable": [], "blocked": [], "stats": {}}
    for it in market_items:
        d = tradable_decision(it, cash=cash)
        (out["tradable"] if d["tradable"] else out["blocked"]).append(d)
    out["stats"] = {"input": len(market_items), "tradable": len(out["tradable"]),
                    "blocked": len(out["blocked"])}
    # 阻止原因分布
    dist = {}
    for d in out["blocked"]:
        for r in d["blocking_reasons"]:
            key = r.split("（")[0].split("超过")[0].split("需")[0][:24]
            dist[key] = dist.get(key, 0) + 1
    out["stats"]["block_distribution"] = dict(sorted(dist.items(), key=lambda x: -x[1])[:12])
    out["fee_scenarios"] = {
        c.config_id: {
            "label": c.name,
            "source": c.source,
            "commission_rate_buy": c.commission_rate_buy,
            "commission_rate_sell": c.commission_rate_sell,
            "commission_min_buy": c.commission_min_buy,
            "commission_min_sell": c.commission_min_sell,
            "stamp_duty_rate_sell": c.stamp_duty_rate_sell,
            "is_etf": c.is_etf,
            "market": c.market,
        } for c in fee_manager.list_configs()
    }
    return out


if __name__ == "__main__":
    from stockpilot.collectors import quote as q
    from stockpilot import load_json
    ml = load_json(REPORTS + "/market_list.json")
    res = screen_tradable(ml["items"])
    save_json(res, REPORTS + "/tradeability.json")
    audit("tradeability_screen", stats=res["stats"])
    print("stats:", res["stats"])
    print("\n可交易样例（前5，含费用敏感性）:")
    for d in res["tradable"][:5]:
        l2 = d["checks"]["L2_tradeability"]
        print(f"  {d['code']} {d['name']} 价{d['price']} 一手{l2['hand_value']} "
              f"计划{l2['planned_shares']}股 买后现金{l2['cash_left_after']}")
        for k, v in l2["fee_scenarios"].items():
            print(f"    {k}: 双边{v['round_trip_total']}元 回本{v['breakeven_pct']}%")
        if d["notes"]:
            print(f"    注意: {'; '.join(d['notes'])}")
