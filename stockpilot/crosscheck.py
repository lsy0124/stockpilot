"""跨数据源核验模块（第四阶段新增）。

任务书 4.2：对关键候选股票用两个独立合法来源核验
代码/名称、交易日期、收盘价、成交量、复权方式、停牌及交易状态。
只有一个来源 → 标记 single_source。
发现冲突 → 不静默选择有利数值，记录双方并标记 conflict。

双源组合（均已实测可用）：
- 源A：新浪实时 hq.sinajs.cn（价格/成交量/名称）
- 源B：新浪日线 CN_MarketDataService（收盘/成交量/复权方式）
- 源C：腾讯实时 qt.gtimg.cn（备用，与新浪互校）
"""
import datetime as dt

from stockpilot import REPORTS, audit, iso, save_json, setup_logger
from stockpilot.collectors import quote as q
from stockpilot import timeutil as tu

log = setup_logger("crosscheck", "crosscheck.log")


def fetch_sina_realtime(full_code):
    """源A：新浪实时"""
    data = q.sina_realtime([full_code])
    return data.get(full_code)


def fetch_sina_daily(full_code, datalen=5):
    """源B：新浪日线（不复权）"""
    bars, meta = q.sina_daily(full_code, datalen=datalen)
    return bars, meta


def fetch_tencent_realtime(full_code):
    """源C：腾讯实时"""
    import requests
    r = requests.get("https://qt.gtimg.cn/q=" + full_code, timeout=20)
    r.encoding = "gbk"
    for line in r.text.splitlines():
        if line.startswith(f"v_{full_code}="):
            f = line.split('"')[1].split("~")
            # 腾讯格式：~名称~代码~当前价~...
            if len(f) > 45:
                return {"name": f[1], "close": float(f[3]), "volume": float(f[36]),
                        "amount": float(f[37]) if f[37] else None,
                        "time": f[30] if len(f) > 30 else None}
    return None


def cross_verify(code, name_hint=None):
    """对单只股票做跨源核验，返回结构化结果"""
    full = code if code.startswith(("sh", "sz")) else (
        f"sh{code}" if code.startswith(("6", "5", "9")) else f"sz{code}")
    result = {
        "code": code, "full_code": full, "checked_at": iso(),
        "sources": {}, "conflicts": [], "verdict": "unknown", "single_source": False,
    }

    # 源A：新浪实时
    try:
        a = fetch_sina_realtime(full)
        if a:
            result["sources"]["sina_realtime"] = {
                "name": a.get("name"), "close": a.get("close"),
                "volume": a.get("volume"), "amount": a.get("amount"),
                "quote_time": a.get("quote_time"), "adjust": "none",
            }
    except Exception as e:
        log.warning("新浪实时失败 %s: %s", code, e)

    # 源B：新浪日线
    try:
        bars, meta = fetch_sina_daily(full, datalen=5)
        if bars:
            result["sources"]["sina_daily"] = {
                "name": name_hint, "close": bars[-1]["close"],
                "volume": bars[-1]["volume"], "date": bars[-1]["date"],
                "adjust": meta.get("adjust"), "source": meta.get("source"),
            }
    except Exception as e:
        log.warning("新浪日线失败 %s: %s", code, e)

    # 源C：腾讯实时（第三源，增强核验）
    try:
        c = fetch_tencent_realtime(full)
        if c:
            result["sources"]["tencent_realtime"] = {
                "name": c.get("name"), "close": c.get("close"),
                "volume": c.get("volume"), "amount": c.get("amount"),
                "time": c.get("time"), "adjust": "none",
            }
    except Exception as e:
        log.warning("腾讯实时失败 %s: %s", code, e)

    # 判定
    closes = {k: v["close"] for k, v in result["sources"].items() if v.get("close")}
    names = {v.get("name") for v in result["sources"].values() if v.get("name")}
    if len(result["sources"]) == 0:
        result["verdict"] = "no_data"
        return result
    if len(result["sources"]) == 1:
        result["single_source"] = True
        result["verdict"] = "single_source"
        return result
    if len(closes) >= 2:
        vals = list(closes.values())
        spread = (max(vals) - min(vals)) / (sum(vals) / len(vals))
        result["price_spread_pct"] = round(spread * 100, 4)
        if spread > 0.005:
            result["conflicts"].append({
                "field": "close", "values": closes,
                "note": "多源价格分歧超过 0.5%，未选择任何一方，需人工复核",
            })
            result["verdict"] = "conflict"
        else:
            result["verdict"] = "pass"
    if len(names) > 1:
        result["conflicts"].append({"field": "name", "values": list(names),
                                    "note": "多源名称不一致"})
        if result["verdict"] == "pass":
            result["verdict"] = "conflict"
    return result


def verify_candidates(candidates):
    """对候选列表批量核验"""
    out = {"verified_at": iso(), "results": [], "summary": {}}
    for c in candidates:
        code = c["code"] if isinstance(c, dict) else c
        r = cross_verify(code)
        out["results"].append(r)
    verdicts = [r["verdict"] for r in out["results"]]
    out["summary"] = {
        "total": len(verdicts),
        "pass": verdicts.count("pass"),
        "conflict": verdicts.count("conflict"),
        "single_source": verdicts.count("single_source"),
        "no_data": verdicts.count("no_data"),
    }
    return out


if __name__ == "__main__":
    # 用最近一次候选实测
    from stockpilot import load_json
    try:
        cands = load_json(REPORTS + "/stock_candidates.json")["candidates"]
        res = verify_candidates(cands)
    except Exception:
        res = verify_candidates(["600028", "600011", "510300"])
    save_json(res, REPORTS + "/cross_verify.json")
    audit("cross_verify_run", summary=res["summary"])
    print("summary:", res["summary"])
    for r in res["results"]:
        print(f"  {r['code']}: {r['verdict']} spread={r.get('price_spread_pct')} "
              f"conflicts={len(r['conflicts'])}")
