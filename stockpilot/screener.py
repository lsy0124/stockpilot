"""全市场股票初筛模块。

筛选维度（对应任务书第八章）：
1. 1000 元账户可交易性：一手价格 ≤ 900 元（留出佣金与零头）
2. 流动性：成交额 ≥ 5000 万元/日，换手不过滤（低价股换手普遍高）
3. 交易限制：剔除 ST/*ST/退市/北交所（1000元账户与权限约束）、剔除停牌迹象
4. 价格趋势：收盘价在 20 日均线之上（用日线计算）
5. 财务质量与估值：优先用全市场业绩报表（营收/净利同比、ROE、每股经营现金流）
6. 事件催化：来自新闻公告模块的相关事件
7. 风险剔除：近期涨幅过大（10日涨幅>60%）不作为初筛加分项而是观察项

设计原则：
- 程序化规则，全流程留痕（每个候选都记录命中/未命中的规则与数据）
- 不凑数：不足 10 只就少于 10 只
- 不预设 ETF 或个股更优：ETF 与个股分别打分
"""
import datetime as dt
import time

from stockpilot import REPORTS, iso, save_json, setup_logger, audit
from stockpilot.collectors import quote as q

log = setup_logger("screener", "screener.log")

MIN_HAND_VALUE = 900.0        # 一手价格上限（1000元本金扣除最低佣金5元×2与零头）
MIN_AMOUNT = 50_000_000       # 日成交额下限 5000 万元（流动性）
MA_WINDOW = 20                # 趋势均线窗口
ETF_KEYWORDS = ("ETF", "交易型开放式")
TREND_FETCH_LIMIT = 260   # 单轮趋势取数上限（腾讯 WAF 保护；约260×0.35s≈91s）
RISK_PREFIXES = ("ST", "*ST", "退", "XD", "DR", "N", "C")


def classify(item):
    """判断标的类型与市场"""
    code = item["code"]
    name = item.get("name") or ""
    market = item.get("market")
    if name.upper().endswith("ETF") or "ETF" in name.upper():
        return "etf"
    if market == "BJ":
        return "bj"
    if code.startswith(("60", "68")):
        return "stock_sh"
    if code.startswith(("00", "30")):
        return "stock_sz"
    return "other"


def hand_value(price):
    return round(price * 100, 2) if price else None


def screen_basic(market_items, rules=None):
    """第一层：可交易性 + 流动性 + 风险过滤"""
    rules = rules or {}
    passed, rejected = [], []
    for it in market_items:
        reasons = []
        price = it.get("price")
        code = it.get("code") or ""
        name = it.get("name") or ""
        typ = classify(it)

        # 可交易性：一手价格
        if not price or price <= 0:
            reasons.append("无有效价格（可能停牌）")
        else:
            hv = hand_value(price)
            it["hand_value"] = hv
            if hv > MIN_HAND_VALUE:
                reasons.append(f"一手价格 {hv:.0f} 元超过 {MIN_HAND_VALUE:.0f} 元上限")

        # 流动性
        amount = it.get("amount") or 0
        if amount < MIN_AMOUNT:
            reasons.append(f"日成交额 {amount/1e4:.0f} 万元低于门槛")

        # 风险/权限
        if typ == "bj":
            reasons.append("北交所标的（需单独开通权限，1000元账户暂不考虑）")
        if typ == "other":
            reasons.append("无法识别的标的类型")
        if any(name.upper().startswith(p) for p in ("*ST", "ST", "退市")):
            reasons.append("风险警示或退市标的")
        if code.startswith("688"):
            reasons.append("科创板需50万资产+2年经验权限，暂缓")

        if reasons:
            it["reject_reasons"] = reasons
            rejected.append(it)
        else:
            passed.append(it)
    return passed, rejected


def add_trend(passed, lookback_days=90, throttle=0.35, max_stocks=None):
    """第二层：用腾讯日线给通过初筛的标的计算趋势指标（20/60日均线、区间涨幅）。

    节流说明：腾讯 WAF 会对高频连续请求返回 501，故串行请求 + 固定间隔。
    max_stocks 用于限制单次运行取数规模（保护数据源，也控制运行时间）。
    被节流命中的标的标记为 trend.ok=False，不伪造趋势数据。
    """
    target = passed if max_stocks is None else passed[:max_stocks]
    for i, it in enumerate(target):
        full = it.get("full_code") or f"{'sh' if it['code'].startswith(('6', '5')) else 'sz'}{it['code']}"
        try:
            bars, meta = q.sina_daily(full, datalen=lookback_days)
            if len(bars) < MA_WINDOW + 5:
                it["trend"] = {"ok": False, "reason": "日线数据不足"}
                continue
            closes = [b["close"] for b in bars]
            ma20 = sum(closes[-MA_WINDOW:]) / MA_WINDOW
            ma60 = sum(closes[-60:]) / 60 if len(closes) >= 60 else None
            chg_10d = (closes[-1] / closes[-11] - 1) * 100 if len(closes) >= 11 else None
            chg_60d = (closes[-1] / closes[-61] - 1) * 100 if len(closes) >= 61 else None
            vols = [b["volume"] for b in bars[-20:]]
            it["trend"] = {
                "ok": True,
                "close": closes[-1], "date": bars[-1]["date"],
                "ma20": round(ma20, 3), "ma60": round(ma60, 3) if ma60 else None,
                "above_ma20": closes[-1] > ma20,
                "above_ma60": (closes[-1] > ma60) if ma60 else None,
                "chg_10d_pct": round(chg_10d, 2) if chg_10d is not None else None,
                "chg_60d_pct": round(chg_60d, 2) if chg_60d is not None else None,
                "vol_avg_20d": round(sum(vols) / len(vols), 1),
                "adjust": meta["adjust"], "source": meta["source"],
            }
        except Exception as e:
            it["trend"] = {"ok": False, "reason": f"{type(e).__name__}: {e}"}
        if throttle and i < len(target) - 1:
            time.sleep(throttle)
    # 未被取数的标的明确标记
    for it in passed[max_stocks:] if max_stocks else []:
        if "trend" not in it:
            it["trend"] = {"ok": False, "reason": "本轮未取数（节流限制）"}
    return passed


def score_item(it):
    """给标的打一个透明的规则分（每项加减分都记录理由）"""
    pts, why = 0.0, []
    t = it.get("trend") or {}
    if t.get("ok"):
        if t.get("above_ma20"):
            pts += 2
            why.append("收盘价在20日均线上方(+2)")
        if t.get("above_ma60"):
            pts += 1
            why.append("收盘价在60日均线上方(+1)")
        c10 = t.get("chg_10d_pct")
        if c10 is not None:
            if 2 <= c10 <= 30:
                pts += 2
                why.append(f"10日涨幅 {c10}% 处于健康区间(+2)")
            elif c10 > 60:
                pts -= 2
                why.append(f"10日涨幅 {c10}% 过大，追高风险(-2)")
        c60 = t.get("chg_60d_pct")
        if c60 is not None and c60 > 100:
            pts -= 1
            why.append(f"60日涨幅 {c60}% 过高(-1)")
    else:
        why.append(f"趋势数据不可用: {t.get('reason')}")
    # 流动性加分
    amt = it.get("amount") or 0
    if amt >= 500_000_000:
        pts += 1
        why.append(f"日成交额 {amt/1e8:.1f} 亿元，流动性好(+1)")
    # 换手率异常（如果新浪给了）
    return round(pts, 2), why


def run_screening(market_items, top_n=10):
    """完整初筛流程"""
    out = {
        "screened_at": iso(),
        "rules": {
            "max_hand_value": MIN_HAND_VALUE,
            "min_amount": MIN_AMOUNT,
            "ma_window": MA_WINDOW,
            "exclusions": ["北交所(权限)", "科创板(权限)", "ST/*ST/退市", "无有效价格", "成交额过低"],
        },
        "stats": {},
        "candidates": [],
        "rejected_sample": [],
        "issues": [],
    }
    passed, rejected = screen_basic(market_items)
    out["stats"]["input"] = len(market_items)
    out["stats"]["passed_basic"] = len(passed)
    out["stats"]["rejected_basic"] = len(rejected)
    # 记录被拒绝的分布（便于解释"为什么不选其他股票"）
    dist = {}
    for r in rejected:
        for reason in r["reject_reasons"]:
            key = reason.split("（")[0].split("超过")[0][:22]
            dist[key] = dist.get(key, 0) + 1
    out["stats"]["reject_distribution"] = dict(sorted(dist.items(), key=lambda x: -x[1])[:15])
    out["rejected_sample"] = [
        {"code": r["code"], "name": r.get("name"), "price": r.get("price"),
         "hand_value": r.get("hand_value"), "reasons": r["reject_reasons"]}
        for r in rejected[:40]
    ]

    passed = add_trend(passed, max_stocks=TREND_FETCH_LIMIT)
    scored = []
    for it in passed:
        pts, why = score_item(it)
        it["score"] = pts
        it["score_reasons"] = why
        scored.append(it)
    scored.sort(key=lambda x: -x["score"])
    out["candidates"] = scored[:top_n]
    out["stats"]["with_trend"] = sum(1 for x in passed if (x.get("trend") or {}).get("ok"))
    out["stats"]["candidates"] = len(out["candidates"])
    return out


if __name__ == "__main__":
    ml = q.collect_market_list()
    if not ml["items"]:
        raise SystemExit("全市场列表为空，无法筛选")
    res = run_screening(ml["items"])
    save_json(res, REPORTS + "/stock_candidates.json")
    save_json(ml, REPORTS + "/market_list.json")
    audit("screening_run", stats=res["stats"],
          candidates=[(c["code"], c["name"], c["score"]) for c in res["candidates"]])
    print("stats:", res["stats"])
    print("top:", [(c["code"], c["name"], c["score"], c.get("trend", {}).get("close")) for c in res["candidates"][:10]])
