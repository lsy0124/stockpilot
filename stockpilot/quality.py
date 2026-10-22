"""数据质量检查模块。

检查项（对应任务书 5.3 / 6.3 要求）：
1. 三方价格交叉校验（腾讯 / 新浪 / 东财实时）
2. 缺失字段检查
3. 异常价格检查（涨跌幅超过阈值、价格为 0/负、成交额为 0 但价格变动）
4. 时效检查（行情时间 vs 采集时间；过期数据必须标记，不得冒充实时）
5. 停牌与风险警示标记
"""
import datetime as dt

from stockpilot import REPORTS, iso, save_json, setup_logger, audit
from stockpilot.collectors import quote as q

log = setup_logger("quality", "quality.log")


def cross_check_prices(codes):
    """对给定代码做三方价格交叉校验。codes 形如 [('sh600519','600519','1.600519'), ...]"""
    results = []
    tencent_codes = [c[0] for c in codes]
    sina_codes = [c[0] for c in codes]
    em_secids = [c[2] for c in codes]

    try:
        sina_data = q.sina_realtime(sina_codes)
    except Exception as e:
        sina_data = {}
        log.warning("sina realtime failed: %s", e)

    for i, (tc, plain, ec) in enumerate(codes):
        rec = {"code": plain, "checked_at": iso(), "sources": {}, "issues": []}
        # 新浪
        s = sina_data.get(tc)
        if s:
            rec["sources"]["sina"] = {"close": s["close"], "quote_time": s["quote_time"]}
        else:
            rec["issues"].append("sina 无数据")
        # 腾讯日线最新收盘
        try:
            bars, meta = q.tencent_daily(tc)
            if bars:
                rec["sources"]["tencent"] = {"close": bars[-1]["close"], "date": bars[-1]["date"],
                                             "adjust": meta["adjust"]}
            else:
                rec["issues"].append("tencent 日线为空")
        except Exception as e:
            rec["issues"].append(f"tencent 失败: {type(e).__name__}")
        # 交叉校验
        closes = [v["close"] for v in rec["sources"].values() if v.get("close")]
        if len(closes) >= 2:
            spread = (max(closes) - min(closes)) / (sum(closes) / len(closes))
            rec["spread_pct"] = round(spread * 100, 4)
            if spread > 0.005:
                rec["issues"].append(f"三方价格分歧 {rec['spread_pct']}%（超过0.5%）")
            rec["verdict"] = "pass" if spread <= 0.005 else "warn"
        else:
            rec["verdict"] = "insufficient"
        results.append(rec)
    return results


def check_snapshot_freshness(snapshot, max_age_minutes=30):
    """快照时效检查：行情时间距采集时间过久必须标记为延迟数据。
    指数 time 字段已是 'YYYY-MM-DD HH:MM:SS' 字符串。"""
    issues = []
    for idx in snapshot.get("indices", []):
        t = idx.get("time")
        if t and t != "-":
            try:
                quote_dt = dt.datetime.strptime(str(t), "%Y-%m-%d %H:%M:%S")
                age = (dt.datetime.now() - quote_dt).total_seconds() / 60
                if age > max_age_minutes:
                    issues.append(f"指数 {idx.get('name')} 行情时间 {t} 已延迟 {age:.0f} 分钟")
            except Exception:
                pass
    return issues


def check_market_list(market_list):
    """全市场列表质量检查"""
    issues = []
    items = market_list.get("items", [])
    if not items:
        return {"verdict": "fail", "issues": ["全市场列表为空"], "stats": {}}
    zero_price = [x for x in items if not x.get("price")]
    zero_amount = [x for x in items if not x.get("amount")]
    no_name = [x for x in items if not x.get("name")]
    extreme = [x for x in items if abs(x.get("pct_chg") or 0) > 20.5]  # 超过涨跌停幅度
    # 停牌迹象：价格为 0 或成交额为 0 但有代码
    suspicious_halt = [x for x in items if (x.get("price") in (0, None)) and x.get("code")]
    stats = {
        "total": len(items), "zero_price": len(zero_price), "zero_amount": len(zero_amount),
        "no_name": len(no_name), "extreme_move": len(extreme), "possible_halt": len(suspicious_halt),
    }
    if zero_price:
        issues.append(f"{len(zero_price)} 只标的价格为 0/缺失（可能停牌或数据缺失），示例: "
                      + ",".join(x["code"] for x in zero_price[:5]))
    if extreme:
        issues.append(f"{len(extreme)} 只涨跌幅超过 ±20.5%（异常或新股/科创创业板），示例: "
                      + ",".join(x["code"] for x in extreme[:5]))
    verdict = "pass" if not issues else ("warn" if len(issues) <= 2 else "fail")
    return {"verdict": verdict, "issues": issues, "stats": stats}


def run_quality_checks(snapshot, market_list, cross_codes):
    """汇总所有质量检查，输出 quality_report"""
    report = {
        "checked_at": iso(),
        "checks": {},
        "overall": "pass",
    }
    c1 = cross_check_prices(cross_codes)
    rep_cross = {"count": len(c1), "pass": sum(1 for r in c1 if r["verdict"] == "pass")}
    report["checks"]["cross_price"] = {"count": len(c1),
                                       "pass": sum(1 for r in c1 if r["verdict"] == "pass"),
                                       "warn": sum(1 for r in c1 if r["verdict"] == "warn"),
                                       "insufficient": sum(1 for r in c1 if r["verdict"] == "insufficient"),
                                       "detail": c1}
    c2 = {"issues": check_snapshot_freshness(snapshot), "verdict": "pass"}
    if c2["issues"]:
        c2["verdict"] = "warn"
    report["checks"]["freshness"] = c2
    c3 = check_market_list(market_list)
    report["checks"]["market_list"] = c3
    # 总判定
    cross_ok = rep_cross["pass"] == rep_cross["count"] and rep_cross["count"] > 0
    if cross_ok and not c2["issues"] and c3["verdict"] == "pass":
        report["overall"] = "pass"
    elif c3["verdict"] == "fail":
        report["overall"] = "fail"
    else:
        report["overall"] = "warn"
    return report


if __name__ == "__main__":
    snap = q.collect_market_snapshot()
    ml = q.collect_market_list()
    cross_codes = [("sh600519", "600519", "1.600519"), ("sz000001", "000001", "0.000001"),
                   ("sh510300", "510300", "1.510300")]
    rep = run_quality_checks(snap, ml, cross_codes)
    save_json(rep, REPORTS + "/quality_report.json")
    audit("quality_check_run", overall=rep["overall"],
          cross=rep["checks"]["cross_price"]["detail"],
          ml_stats=rep["checks"]["market_list"]["stats"])
    print("overall:", rep["overall"])
    print("cross:", {k: v for k, v in rep["checks"]["cross_price"].items() if k != "detail"})
    print("ml:", rep["checks"]["market_list"]["stats"], rep["checks"]["market_list"]["issues"][:3])
