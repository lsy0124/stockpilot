"""智谱独立研究与反方审查（第四阶段升级）。

任务书第八章要求：
- 智谱先独立形成研究意见（市场环境/行业机会/候选/风险等级/正反证据/待验证问题）
- 反方审查主动寻找：重大负面公告、财务质量问题、估值过高、利好已反映在价格中、
  数据来源冲突、流动性不足、交易成本过高、历史类似策略失败、现金/其他标的更合适
- 必须区分：事实（fact）/ 计算结果（computed）/ 模型判断（judgment）/ 未知（unknown）
- 不得把模型主观观点伪装成独立证据

第四阶段新增：
1. 财报维度接入（akshare stock_yjbb_em：营收/净利同比、ROE、每股经营现金流、毛利率、披露日期）
2. 证据四分类标注
3. 反方审查新增数据冲突检查、历史策略失败检查、财务与估值检查
"""
import datetime as dt

from stockpilot import REPORTS, FEE_MODEL, iso, save_json, setup_logger, audit
from stockpilot.collectors import quote as q
from stockpilot import timeutil as tu

log = setup_logger("research", "research.log")


# ---------------------------------------------------------------- 费用计算器
def fee_calculator(price, shares=100, is_etf=False, fee_model=None):
    """可配置费用计算器。默认以最低佣金5元做压力测试假设（非用户真实券商费率）。"""
    fm = dict(FEE_MODEL)
    if fee_model:
        fm.update(fee_model)
    buy_amount = round(price * shares, 2)
    sell_amount = buy_amount
    buy_fee = max(buy_amount * fm["commission_rate"], fm["commission_min"])
    sell_fee = max(sell_amount * fm["commission_rate"], fm["commission_min"])
    stamp = 0.0 if (is_etf and fm["stamp_duty_etf_exempt"]) else sell_amount * fm["stamp_duty_rate"]
    transfer = (buy_amount + sell_amount) * fm["transfer_fee_rate"]
    total = round(buy_fee + sell_fee + stamp + transfer, 2)
    return {
        "shares": shares, "price": price, "position_value": buy_amount,
        "buy_commission": round(buy_fee, 2), "sell_commission": round(sell_fee, 2),
        "stamp_duty": round(stamp, 2), "transfer_fee": round(transfer, 2),
        "round_trip_total": total,
        "pct_of_capital": round(total / 1000 * 100, 3),
        "breakeven_pct": round(total / buy_amount * 100, 3),
        "is_etf": is_etf, "fee_model": fm,
        "note": "最低佣金5元为压力测试假设，实际费率须在交易前向券商核实",
    }


# ---------------------------------------------------------------- 财报数据
def fetch_fundamentals(codes=None, report_date="20260630"):
    """拉取全市场业绩报表，提取指定代码的财务指标。

    返回 {code: {指标...}}。失败返回 {} 与原因（由调用方披露）。
    注意：这是最新报告期数据，禁止用于历史时点判断（披露时间见 latest_announcement_date）。
    """
    out = {"data": {}, "meta": {"ok": False, "error": None, "report_date": report_date}}
    if not codes:
        return out
    try:
        import akshare as ak
        df = ak.stock_yjbb_em(date=report_date)
        cols = {"股票代码": "code", "股票简称": "name", "每股收益": "eps",
                "营业总收入-营业总收入": "revenue",
                "营业总收入-同比增长": "revenue_yoy",
                "净利润-净利润": "net_profit", "净利润-同比增长": "profit_yoy",
                "每股净资产": "bvps", "净资产收益率": "roe",
                "每股经营现金流量": "ocf_per_share", "销售毛利率": "gross_margin",
                "所处行业": "industry", "最新公告日期": "disclosure_date"}
        df = df.rename(columns=cols)
        want = set(codes)
        sub = df[df["code"].astype(str).isin(want)]
        for _, r in sub.iterrows():
            out["data"][str(r["code"])] = {
                "name": r.get("name"), "eps": r.get("eps"),
                "revenue": r.get("revenue"), "revenue_yoy": r.get("revenue_yoy"),
                "net_profit": r.get("net_profit"), "profit_yoy": r.get("profit_yoy"),
                "bvps": r.get("bvps"), "roe": r.get("roe"),
                "ocf_per_share": r.get("ocf_per_share"),
                "gross_margin": r.get("gross_margin"), "industry": r.get("industry"),
                "disclosure_date": str(r.get("disclosure_date")),
            }
        out["meta"] = {"ok": True, "report_date": report_date,
                       "fetched_at": iso(), "rows_in_report": len(df),
                       "matched": len(out["data"]),
                       "source": "akshare.stock_yjbb_em",
                       "adjust": "未复权（财务数据）",
                       "caveat": "为最新报告期数据；历史研究必须使用当时已披露的版本，"
                                 "本数据不得用于回溯判断"}
    except Exception as e:
        out["meta"]["error"] = f"{type(e).__name__}: {e}"
        log.warning("财报拉取失败: %s", e)
    return out


def classify_evidence(kind, text, source=None):
    """证据四分类：fact(事实) / computed(计算结果) / judgment(模型判断) / unknown(未知)"""
    return {"type": kind, "text": text, "source": source}


# ---------------------------------------------------------------- 独立研究
def build_research(snapshot, market_list, candidates, news, quality, fundamentals=None,
                   cross_verify=None, tradeability=None):
    """智谱独立研究（不知道 GPT-6 结论的前提下形成）"""
    idx = {i["name"]: i for i in snapshot.get("indices", [])}
    breadth = market_list.get("breadth", {})
    boards = sorted(snapshot.get("boards", []), key=lambda b: -(b.get("pct_chg") or 0))
    weakest = sorted(snapshot.get("boards", []), key=lambda b: (b.get("pct_chg") or 0))[:5]

    env = []
    sh = idx.get("上证指数", {})
    if sh:
        env.append(classify_evidence(
            "fact", f"上证指数收 {sh.get('close')}（{sh.get('pct_chg')}%），数据时间 {sh.get('time')}"))
    if breadth:
        env.append(classify_evidence(
            "fact", f"全市场 {breadth.get('total')} 只中上涨 {breadth.get('up')} 只、"
                    f"下跌 {breadth.get('down')} 只，涨跌比 "
                    f"{(breadth.get('up', 0) / max(breadth.get('down', 1), 1)):.2f}"))

    cross_map = {}
    if cross_verify:
        cross_map = {r["code"]: r for r in cross_verify.get("results", [])}
    fund_map = (fundamentals or {}).get("data", {})

    enriched = []
    for c in candidates:
        t = c.get("trend") or {}
        price = t.get("close") or c.get("price") or 0
        name = c.get("name") or ""
        is_etf = name.upper().endswith("ETF")
        fee = fee_calculator(price, shares=100, is_etf=is_etf)
        bull, bear = [], []

        # 趋势证据（computed：由价格序列计算）
        if t.get("ok"):
            if t.get("above_ma20"):
                bull.append(classify_evidence("computed",
                    f"收盘 {t['close']} 站上20日均线 {t['ma20']}（源：{t.get('source')}）"))
            if t.get("above_ma60"):
                bull.append(classify_evidence("computed", f"收盘在60日均线 {t['ma60']} 上方"))
            c10 = t.get("chg_10d_pct")
            if c10 is not None:
                if 2 <= c10 <= 30:
                    bull.append(classify_evidence("computed", f"10日涨幅 {c10}% 处于健康区间"))
                elif c10 > 60:
                    bear.append(classify_evidence("computed", f"10日涨幅 {c10}% 过大，追高风险"))
                elif c10 < -20:
                    bear.append(classify_evidence("computed", f"10日跌幅 {abs(c10)}%，趋势偏弱"))
            if not t.get("above_ma20"):
                bear.append(classify_evidence("computed", f"收盘 {t['close']} 低于20日均线 {t['ma20']}"))
        else:
            bear.append(classify_evidence("unknown", f"趋势数据不可用：{t.get('reason')}"))

        # 流动性
        amt = c.get("amount") or 0
        if amt >= 500_000_000:
            bull.append(classify_evidence("fact", f"日成交额 {amt/1e8:.1f} 亿元，流动性充足"))
        elif amt >= 50_000_000:
            bull.append(classify_evidence("fact", f"日成交额 {amt/1e8:.1f} 亿元，满足最低流动性门槛"))
        else:
            bear.append(classify_evidence("fact", f"日成交额 {amt/1e4:.0f} 万元，流动性不足"))

        # 财务证据（fact：来自业绩报表；judgment：模型解读）
        f = fund_map.get(c["code"])
        if f:
            yoy = f.get("revenue_yoy")
            pyoy = f.get("profit_yoy")
            roe = f.get("roe")
            ocf = f.get("ocf_per_share")
            if yoy is not None and yoy == yoy:
                (bull if yoy > 0 else bear).append(classify_evidence(
                    "fact", f"最新报告期营收同比 {yoy:.2f}%（披露日 {f.get('disclosure_date')}）"))
            if pyoy is not None and pyoy == pyoy:
                (bull if pyoy > 0 else bear).append(classify_evidence(
                    "fact", f"净利润同比 {pyoy:.2f}%"))
            if roe is not None and roe == roe:
                (bull if roe >= 8 else bear).append(classify_evidence(
                    "fact", f"ROE {roe:.2f}%"))
            if ocf is not None and ocf == ocf:
                (bull if ocf > 0 else bear).append(classify_evidence(
                    "fact", f"每股经营现金流 {ocf:.3f} 元" +
                    ("" if ocf > 0 else "（为负，盈利质量需关注）")))
            # 估值粗判（judgment）
            if roe is not None and roe == roe and roe >= 12:
                bull.append(classify_evidence("judgment",
                    f"ROE {roe:.1f}% 处于较高水平，若估值未极端偏高则具备质量特征"
                    f"（未取得实时PE/PB，属未验证项）"))
        else:
            bear.append(classify_evidence("unknown",
                f"未取得 {c['code']} 最新业绩报表数据（财务维度缺失）"))

        # 跨源核验结果（fact）
        cv = cross_map.get(c["code"])
        if cv:
            if cv["verdict"] == "conflict":
                bear.append(classify_evidence("fact",
                    f"多源数据存在冲突：{cv['conflicts'][0]['note'] if cv['conflicts'] else '未知字段'}"))
            elif cv["verdict"] == "single_source":
                bear.append(classify_evidence("fact", "仅单源数据，未经交叉核验"))

        # 成本（computed）
        if fee["breakeven_pct"] > 1.5:
            bear.append(classify_evidence("computed",
                f"双边成本 {fee['round_trip_total']} 元，回本需涨 {fee['breakeven_pct']}%，"
                f"高于 1.5% 阈值"))

        enriched.append({
            **{k: c.get(k) for k in ("code", "name", "price", "hand_value", "amount", "score")},
            "trend": t, "fee": fee, "fundamentals": f,
            "bull_evidence": bull, "bear_evidence": bear,
            "cross_verify": cv,
        })

    research = {
        "generated_at": iso(),
        "author": "GLM5.3（智谱独立研究）",
        "data_as_of": {
            "trade_date": snapshot.get("trade_date"),
            "market_list_collected": market_list.get("collected_at"),
            "quality_overall": quality.get("overall"),
            "fundamentals_meta": (fundamentals or {}).get("meta"),
        },
        "market_environment": env,
        "strongest_boards": [{"name": b["name"], "pct_chg": b["pct_chg"],
                              "lead_stock": b.get("lead_stock"),
                              "up": b.get("up_count"), "down": b.get("down_count")}
                             for b in boards[:6]],
        "weakest_boards": [{"name": b["name"], "pct_chg": b["pct_chg"]} for b in weakest],
        "candidates": enriched,
        "evidence_legend": {
            "fact": "来自数据源的客观记录（价格/公告/财报数字）",
            "computed": "由程序计算得出的结果（均线/涨幅/费用）",
            "judgment": "智谱模型的主观判断（非独立证据，需人工复核）",
            "unknown": "数据缺失或未能验证的事项",
        },
        "risks": [],
        "data_gaps": [],
        "questions_to_verify": [],
    }

    if quality.get("overall") != "pass":
        research["risks"].append(classify_evidence(
            "fact", f"数据质量总判定为 {quality.get('overall')}，可能影响结论可靠性"))
    if snapshot.get("data_issues"):
        research["risks"].append(classify_evidence(
            "fact", f"快照采集存在 {len(snapshot['data_issues'])} 项问题"))
    no_trend = [c["code"] for c in candidates if not (c.get("trend") or {}).get("ok")]
    if no_trend:
        research["data_gaps"].append(classify_evidence(
            "unknown", f"{len(no_trend)} 只候选无趋势数据: {no_trend}"))
    no_fund = [c["code"] for c in candidates if c["code"] not in fund_map]
    if no_fund:
        research["data_gaps"].append(classify_evidence(
            "unknown", f"{len(no_fund)} 只候选无财报数据（财务维度缺失）"))
    # 待验证问题（由数据缺口驱动）
    research["questions_to_verify"].append(classify_evidence(
        "judgment", "候选的实时 PE/PB 未取得，估值高低只能定性判断，需补充估值数据"))
    research["questions_to_verify"].append(classify_evidence(
        "judgment", "行业强弱基于单日涨跌幅，未看多日趋势，行业判断可能片面"))
    return research


# ---------------------------------------------------------------- 反方审查
def build_adversarial(research_input, market_list, snapshot, news, quality,
                      fundamentals=None, cross_verify=None, tradeability=None,
                      failed_strategies=None):
    """反方审查：独立数据 + 明确规则，不读研究结论。"""
    checks = []
    fund_map = (fundamentals or {}).get("data", {})

    # 检查1：重大负面新闻扫描
    risk_words = ("减持", "处罚", "诉讼", "退市", "亏损", "下调", "问询", "违规", "终止",
                  "跌停", "爆仓", "违约", "商誉", "冻结", "退市风险", "立案")
    neg = [{"title": (e.get("title") or "")[:60], "code": e.get("sec_code"),
            "published": e.get("published_at"), "source": e.get("source")}
           for e in news.get("events", []) if any(w in (e.get("title") or "") for w in risk_words)]
    checks.append({"check": "重大负面公告/新闻扫描",
                   "method": "对全部采集事件按风险词表扫描（减持/处罚/诉讼/退市/亏损/立案等）",
                   "result": f"命中 {len(neg)} 条风险相关事件",
                   "verdict": "warn" if neg else "pass", "evidence": neg[:10]})

    # 检查2：数据时效
    stale = quality.get("checks", {}).get("freshness", {}).get("issues", [])
    checks.append({"check": "数据时效",
                   "method": "指数行情时间与采集时间差 > 30 分钟（交易时段内）即标记",
                   "result": "; ".join(stale) if stale else "行情时间在允许范围内（非交易时段属正常）",
                   "verdict": "warn" if stale else "pass", "evidence": stale[:5]})

    # 检查3：1000元可交易性（用 tradeability 的正式判定）
    violations = []
    ta_map = {d["code"]: d for d in (tradeability or {}).get("tradable", [])}
    for c in research_input:
        code = c["code"] if isinstance(c, dict) else c
        d = ta_map.get(code)
        if d is None:
            # 不在可交易集 → 找阻止原因
            for b in (tradeability or {}).get("blocked", []):
                if b["code"] == code:
                    violations.append(f"{code} 不可交易: {b['blocking_reasons'][0]}")
                    break
            else:
                hv = c.get("hand_value") if isinstance(c, dict) else None
                if hv and hv > 900:
                    violations.append(f"{code} 一手价格 {hv} 元超过 900 元上限")
    checks.append({"check": "1000元可交易性约束",
                   "method": "分层筛选器判定：一手价格≤900元、现金含费用充足、非权限限制品种",
                   "result": f"{len(violations)} 只违反" if violations else "全部候选满足",
                   "verdict": "fail" if violations else "pass", "evidence": violations[:10]})

    # 检查4：交易成本（多费率敏感性）
    from stockpilot.tradeability import fee_scenarios_for
    high_cost = []
    for c in research_input:
        if not isinstance(c, dict):
            continue
        price = (c.get("trend") or {}).get("close") or c.get("price") or 0
        if price <= 0:
            continue
        is_etf = (c.get("name") or "").upper().endswith("ETF")
        sc = fee_scenarios_for(price, 100, is_etf)
        if sc["pessimistic_5yuan"]["breakeven_pct"] > 1.5:
            high_cost.append(
                f"{c['code']} 悲观费率回本需涨 {sc['pessimistic_5yuan']['breakeven_pct']}%"
                f"（乐观无最低佣金时 {sc['optimistic_no_min']['breakeven_pct']}%）")
    checks.append({"check": "交易成本（多费率敏感性）",
                   "method": "按最低佣金5元压力假设计算双边成本，回本涨幅>1.5%视为过高；"
                             "同时给出乐观费率对照",
                   "result": f"{len(high_cost)} 只成本偏高" if high_cost else "全部候选回本要求≤1.5%",
                   "verdict": "warn" if high_cost else "pass", "evidence": high_cost[:10]})

    # 检查5：数据来源冲突（跨源核验）
    conflicts = []
    for r in (cross_verify or {}).get("results", []):
        if r["verdict"] == "conflict":
            conflicts.append(f"{r['code']}: {r['conflicts'][0]['note'] if r['conflicts'] else '字段冲突'}")
        elif r["verdict"] == "single_source":
            conflicts.append(f"{r['code']}: 仅单源数据，未交叉核验")
    checks.append({"check": "数据来源冲突",
                   "method": "对每只候选用新浪实时+新浪日线+腾讯实时三源核验价格/名称，"
                             "分歧>0.5%标记冲突",
                   "result": f"{len(conflicts)} 只存在冲突或单源" if conflicts else "全部候选多源一致",
                   "verdict": "warn" if conflicts else "pass", "evidence": conflicts[:10]})

    # 检查6：财务质量
    fin_issues = []
    for c in research_input:
        if not isinstance(c, dict):
            continue
        f = fund_map.get(c["code"])
        if not f:
            fin_issues.append(f"{c['code']} 无财报数据（无法评估财务质量）")
            continue
        pyoy = f.get("profit_yoy")
        roe = f.get("roe")
        ocf = f.get("ocf_per_share")
        if pyoy is not None and pyoy == pyoy and pyoy < -20:
            fin_issues.append(f"{c['code']} 净利润同比 {pyoy:.1f}%，大幅下滑")
        if roe is not None and roe == roe and roe < 5:
            fin_issues.append(f"{c['code']} ROE {roe:.1f}%，偏低")
        if ocf is not None and ocf == ocf and ocf < 0:
            fin_issues.append(f"{c['code']} 每股经营现金流 {ocf:.3f} 元为负")
    checks.append({"check": "财务质量",
                   "method": "对每只候选检查净利润同比、ROE、每股经营现金流"
                             "（阈值：净利同比<-20%、ROE<5%、OCF<0）",
                   "result": f"{len(fin_issues)} 只存在财务疑点" if fin_issues else "未发现财务疑点",
                   "verdict": "warn" if fin_issues else "pass", "evidence": fin_issues[:10]})

    # 检查7：利好是否已反映在价格中
    reflected = []
    for c in research_input:
        if not isinstance(c, dict):
            continue
        t = c.get("trend") or {}
        c10 = t.get("chg_10d_pct")
        c60 = t.get("chg_60d_pct")
        if c10 is not None and c10 > 30:
            reflected.append(f"{c['code']} 10日已涨 {c10}%，短期利好可能已反映")
        if c60 is not None and c60 > 80:
            reflected.append(f"{c['code']} 60日已涨 {c60}%，中期涨幅已大")
    checks.append({"check": "利好是否已反映在股价中",
                   "method": "检查10日涨幅>30% 或60日涨幅>80% 的候选",
                   "result": f"{len(reflected)} 只涨幅已大" if reflected else "候选短期涨幅未达极端水平",
                   "verdict": "warn" if reflected else "pass", "evidence": reflected[:10]})

    # 检查8：历史类似策略失败
    from stockpilot import ledger as L
    hist_fail = []
    if failed_strategies:
        hist_fail = failed_strategies
    else:
        # 从预测复盘中提取失败案例
        try:
            preds = L.get_predictions(resolved=True, limit=50)
            for p in preds:
                o = p.get("outcome")
                if isinstance(o, str):
                    import json as _j
                    try:
                        o = _j.loads(o)
                    except Exception:
                        continue
                if isinstance(o, dict) and o.get("direction_correct") is False:
                    hist_fail.append(
                        f"预测#{p['id']}({p.get('code') or p.get('subject')})方向错误："
                        f"实际 {o.get('change_pct')}%")
        except Exception as e:
            log.warning("历史失败案例提取失败: %s", e)
    checks.append({"check": "历史类似判断失败",
                   "method": "从预测复盘中提取方向错误的已结算预测",
                   "result": f"{len(hist_fail)} 条历史错误预测" if hist_fail else "无已结算的错误方向预测",
                   "verdict": "warn" if hist_fail else "pass", "evidence": hist_fail[:10]})

    # 检查9：现金/ETF替代
    try:
        bars, _ = q.sina_daily("sh510300", datalen=60)
        etf_10d = round((bars[-1]["close"] / bars[-11]["close"] - 1) * 100, 2) if len(bars) >= 11 else None
        etf_close = bars[-1]["close"] if bars else None
    except Exception:
        etf_10d, etf_close = None, None
    checks.append({"check": "是否存在更简单的现金/ETF替代",
                   "method": "对比沪深300ETF(510300)近10日表现与零成本现金持有选项",
                   "result": f"510300 近10日 {etf_10d}%（现价 {etf_close}）；现金持有成本为0，"
                             f"且不承担选股错误风险。若候选预期收益不明显高于ETF，"
                             f"应优先考虑ETF或空仓",
                   "verdict": "info",
                   "evidence": [{"etf": "510300", "chg_10d_pct": etf_10d, "close": etf_close}]})

    fails = [c for c in checks if c["verdict"] == "fail"]
    warns = [c for c in checks if c["verdict"] == "warn"]
    return {
        "generated_at": iso(),
        "author": "GLM5.3（反方审查，独立数据与规则）",
        "independence_note": "本审查未读取研究结论文字；仅使用原始采集数据与预定义规则，"
                             "与研究模块相互独立。",
        "checks": checks,
        "summary": {
            "fail": len(fails), "warn": len(warns),
            "conclusion": ("存在硬性违规，建议暂缓交易" if fails else
                           ("存在需关注事项，建议降低预期或缩小范围" if warns else
                            "未发现规则层面的硬性障碍；但不构成买入建议")),
        },
    }


if __name__ == "__main__":
    from stockpilot import load_json
    snap = load_json(REPORTS + "/market_snapshot.json")
    ml = load_json(REPORTS + "/market_list.json")
    cands = load_json(REPORTS + "/stock_candidates.json")["candidates"]
    news = load_json(REPORTS + "/news_events.json")
    qual = load_json(REPORTS + "/quality_report.json")
    codes = [c["code"] for c in cands]
    fund = fetch_fundamentals(codes)
    print("财报匹配:", fund["meta"].get("matched"), "/", len(codes), "| 错误:", fund["meta"].get("error"))
    cv = load_json(REPORTS + "/cross_verify.json")
    ta = load_json(REPORTS + "/tradeability.json")
    research = build_research(snap, ml, cands, news, qual, fund, cv, ta)
    save_json(research, REPORTS + "/research.json")
    adv = build_adversarial(cands, ml, snap, news, qual, fund, cv, ta)
    save_json(adv, REPORTS + "/risk_review.json")
    print("研究候选:", len(research["candidates"]), "| 数据缺口:", len(research["data_gaps"]))
    print("反方:", adv["summary"])
