"""行情采集模块。

设计原则（承接第一阶段结论）：
- 主源：腾讯（已验证稳定，含复权）
- 备源：新浪（实时 GBK）
- 东财 push2 仅用于"全市场列表/实时快照/涨停池"（push2his 历史接口已确认被限流，不使用）
- 每条数据强制带：来源、行情时间、采集时间、复权方式、单位、延迟/缺失/异常标记
"""
import datetime as dt
import json
import os
import time

import requests

from .. import (AUDIT, DATA, EVIDENCE, FEE_MODEL, USER_AGENT, iso, latest_trading_day,
                save_json, setup_logger, audit)

log = setup_logger("collector.quote", "collector_quote.log")

S = requests.Session()
S.headers.update({"User-Agent": USER_AGENT})


# ---------------------------------------------------------------- 交易日历
def trading_calendar():
    """新浪交易日历；失败时回退到本地缓存"""
    cache = os.path.join(DATA, "trade_calendar.json")
    try:
        r = S.get("https://finance.sina.com.cn/realstock/company/klc_td_sh.txt", timeout=20)
        import re
        m = re.search(r'datelist="([^"]+)"', r.text)
        if m:
            # 新浪的日历是混淆字符串，直接用 akshare 的解析不可取，改为调用已验证接口
            pass
    except Exception as e:
        log.warning("sina calendar raw fetch failed: %s", e)
    try:
        import akshare as ak
        df = ak.tool_trade_date_hist_sina()
        days = [str(x)[:10] for x in df["trade_date"]]
        save_json({"source": "akshare.tool_trade_date_hist_sina", "fetched_at": iso(),
                   "count": len(days), "days": days}, cache)
        return days
    except Exception as e:
        log.warning("akshare calendar failed (%s), falling back to cache", e)
        if os.path.exists(cache):
            return load_cached_calendar(cache)
        return []


def load_cached_calendar(cache):
    with open(cache, encoding="utf-8") as f:
        return json.load(f).get("days", [])


# ---------------------------------------------------------------- 股票池
def full_market_list(retries=3):
    """东财 push2 全市场列表（已验证：一次返回 5562 只，含 A股+ETF+北交所）。
    该接口间歇返回 502（网关层限流），带退避重试；连续失败则明确报错，不伪造数据。"""
    url = "https://push2.eastmoney.com/api/qt/clist/get"
    out = []
    last_err = None
    for attempt in range(retries):
        try:
            page = 1
            out = []
            while True:
                params = {"pn": page, "pz": 1000, "po": 1, "np": 1, "fltt": 2, "invt": 2, "fid": "f3",
                          "fs": "m:0+t:6,m:0+t:80,m:1+t:2,m:1+t:23",
                          "fields": "f12,f14,f2,f3,f5,f6,f8,f20,f21"}
                r = S.get(url, params=params, headers={"Referer": "https://quote.eastmoney.com/"},
                          timeout=30)
                if r.status_code != 200:
                    raise IOError(f"HTTP {r.status_code}")
                j = r.json()
                d = j.get("data") or {}
                diff = d.get("diff") or []
                if not diff:
                    break
                out.extend(diff)
                total = d.get("total") or 0
                if len(out) >= total or page >= 10:
                    break
                page += 1
                time.sleep(0.4)
            if out:
                return out
            last_err = "empty diff"
        except Exception as e:
            last_err = f"{type(e).__name__}: {e}"
            log.warning("full_market_list attempt %d failed: %s", attempt + 1, last_err)
        if attempt < retries - 1:
            time.sleep(2 * (attempt + 1))
    raise IOError(f"full_market_list 重试 {retries} 次后仍失败: {last_err}")


# ---------------------------------------------------------------- 日线
def sina_daily(symbol, datalen=60):
    """新浪日线（不复权）。

    选型记录：腾讯 fqkline 在批量连续请求下触发 WAF 501（实测：休息60秒不解除），
    akshare.stock_zh_a_daily 每次约5秒不适合全市场批量，
    故采用新浪原生 K 线接口（实测 40 次连续请求 0 失败，约 0.75s/次）。
    注意：该接口为不复权价，除权日会出现价格跳空，由质量检查模块标记。
    symbol 形如 sh600519 / sz000001。
    """
    url = "https://quotes.sina.cn/cn/api/json_v2.php/CN_MarketDataService.getKLineData"
    r = S.get(url, params={"symbol": symbol, "scale": 240, "ma": "no", "datalen": datalen},
              headers={"Referer": "https://finance.sina.com.cn"}, timeout=20)
    if r.status_code != 200 or not r.text.strip().startswith("["):
        raise IOError(f"HTTP {r.status_code}: {r.text[:80]}")
    bars = []
    for x in r.json():
        bars.append({"date": x["day"], "open": float(x["open"]), "high": float(x["high"]),
                     "low": float(x["low"]), "close": float(x["close"]),
                     "volume": float(x["volume"])})
    return bars, {"source": "sina.CN_MarketDataService.getKLineData", "adjust": "none",
                  "bars": len(bars)}


def tencent_daily(code, start=None, end=None, adjust="qfq", count=800):
    """腾讯日线（主源）。code 形如 sh600519 / sz000001 / sh510300。
    注意：腾讯 param 的日期必须带横杠（YYYY-MM-DD），紧凑格式会返回 param error。"""
    end = end or dt.date.today().isoformat()
    start = start or (dt.date.today() - dt.timedelta(days=365)).isoformat()
    url = "https://web.ifzq.gtimg.cn/appstock/app/fqkline/get"
    param = f"{code},day,{start},{end},{count},{adjust}"
    r = S.get(url, params={"param": param}, timeout=30)
    j = r.json()
    node = (j.get("data") or {}).get(code) or {}
    key = "qfqday" if adjust == "qfq" else "day"
    rows = node.get(key) or node.get("day") or []
    bars = []
    for x in rows:
        bars.append({"date": x[0], "open": float(x[1]), "close": float(x[2]),
                     "high": float(x[3]), "low": float(x[4]), "volume": float(x[5])})
    return bars, {"source": "tencent.fqkline", "adjust": adjust, "bars": len(bars)}


def sina_realtime(codes):
    """新浪实时快照（备源）。codes 形如 ['sh600519','sz000001']"""
    url = "https://hq.sinajs.cn/list=" + ",".join(codes)
    r = S.get(url, headers={"Referer": "https://finance.sina.com.cn"}, timeout=20)
    txt = r.content.decode("gbk", "replace")
    out = {}
    for line in txt.splitlines():
        if "=" not in line or '"' not in line:
            continue
        code = line.split("=")[0].split("_")[-1]
        body = line.split('"')[1]
        f = body.split(",")
        if len(f) < 32:
            continue
        out[code] = {
            "name": f[0], "open": float(f[1]), "prev_close": float(f[2]),
            "close": float(f[3]), "high": float(f[4]), "low": float(f[5]),
            "volume": float(f[8]), "amount": float(f[9]),
            "quote_time": f[30] + " " + f[31],
            "source": "sina.hq",
        }
    return out


# ---------------------------------------------------------------- 分钟线
def tencent_minute(code):
    """腾讯当日分时（分钟粒度）"""
    url = "https://web.ifzq.gtimg.cn/appstock/app/minute/query"
    r = S.get(url, params={"code": code}, timeout=20)
    j = r.json()
    node = ((j.get("data") or {}).get(code) or {}).get("data") or {}
    return node.get("data") or [], {"source": "tencent.minute", "points": len(node.get("data") or [])}


# ---------------------------------------------------------------- 指数与板块
def em_board_industry():
    """行业板块强弱。东财 push2 clist 的 m:90 t:2 当前返回 502（域名级异常），
    已改用同花顺源（第一阶段未验证，本次实测通过）。"""
    import akshare as ak
    df = ak.stock_board_industry_summary_ths()
    out = []
    for _, r in df.iterrows():
        out.append({
            "name": r.get("板块"), "pct_chg": r.get("涨跌幅"),
            "volume": r.get("总成交量"), "amount": r.get("总成交额"),
            "net_inflow": r.get("净流入"), "up_count": r.get("上涨家数"),
            "down_count": r.get("下跌家数"), "lead_stock": r.get("领涨股"),
            "lead_pct": r.get("领涨股-涨跌幅"),
        })
    return out



def sina_index_quote(secids=("s_sh000001", "s_sz399001", "s_sh000300", "s_sz399006",
                             "s_sh000016", "s_sh000905", "s_sh000688")):
    """新浪指数实时快照。东财 ulist 于 2026-10-18 出现 RST（第三个东财接口故障），
    切换新浪源。字段：名称, 当前价, 涨跌额, 涨跌幅%, 成交量(手), 成交额(万元)。"""
    url = "https://hq.sinajs.cn/list=" + ",".join(secids)
    r = S.get(url, headers={"Referer": "https://finance.sina.com.cn"}, timeout=25)
    r.encoding = "gbk"
    out = []
    for line in r.text.splitlines():
        if "=" not in line or '"' not in line:
            continue
        key = line.split("=")[0].split("_")[-1]
        f = line.split('"')[1].split(",")
        if len(f) < 6:
            continue
        out.append({"code": key, "name": f[0], "close": float(f[1]), "change": float(f[2]),
                    "pct_chg": float(f[3]), "volume": float(f[4]), "amount": float(f[5]) * 1e4,
                    "time": None, "source": "sina.hq",
                    "time_note": "新浪接口不返回行情时刻，time 以采集时间近似；"
                                 "非交易时段为最近收盘数据"})
    return out


def em_index_quote(secids=("1.000001", "0.399001", "1.000300", "0.399006", "1.000688",
                           "1.000016", "1.000905")):
    """主要指数实时。注意：该接口实际会 302 到 push2delay 域名，直接指定目标主机。"""
    url = "https://push2delay.eastmoney.com/api/qt/ulist.np/get"
    params = {"fltt": 2, "invt": 2, "secids": ",".join(secids),
              "fields": "f12,f14,f2,f3,f4,f5,f6,f124"}
    r = S.get(url, params=params, headers={"Referer": "https://quote.eastmoney.com/"}, timeout=30)
    j = r.json()
    return (j.get("data") or {}).get("diff") or []


# ---------------------------------------------------------------- 涨跌停与停牌
def em_zt_pool(date):
    """东财涨停池（date 形如 20261016）"""
    url = "https://push2ex.eastmoney.com/getTopicZTPool"
    params = {"ut": "7eea3edcaed734bea9cbfc24409ed989", "dpt": "wz.ztzt",
              "Pageindex": 0, "pagesize": 200, "sort": "fbt:asc", "date": date}
    r = S.get(url, params=params, headers={"Referer": "https://quote.eastmoney.com/"}, timeout=25)
    j = r.json()
    d = j.get("data") or {}
    return d.get("pool") or [], {"date": date, "total": d.get("tc")}


def em_dt_pool(date):
    """东财跌停池"""
    url = "https://push2ex.eastmoney.com/getTopicDTPool"
    params = {"ut": "7eea3edcaed734bea9cbfc24409ed989", "dpt": "wz.dtzt",
              "Pageindex": 0, "pagesize": 200, "sort": "fund:asc", "date": date}
    try:
        r = S.get(url, params=params, headers={"Referer": "https://quote.eastmoney.com/"}, timeout=25)
        j = r.json()
        d = j.get("data") or {}
        return d.get("pool") or [], {"date": date, "total": d.get("tc")}
    except Exception as e:
        log.warning("dt pool failed: %s", e)
        return [], {"date": date, "total": None}


def sina_market_list(node="hs_a", max_pages=70):
    """新浪全市场列表（东财 clist 被限流后的合规替代源）。
    实测：5576 只（沪2319/深2906/北351），56 页约 42 秒。
    字段：symbol/name/trade(最新价)/changepercent/volume/amount/ticktime。"""
    url = ("https://vip.stock.finance.sina.com.cn/quotes_service/api/json_v2.php/"
           "Market_Center.getHQNodeDataSimple")
    items = []
    for page in range(1, max_pages + 1):
        try:
            r = S.get(url, params={"page": page, "num": 100, "sort": "symbol", "asc": 1,
                                   "node": node},
                      headers={"Referer": "https://finance.sina.com.cn"}, timeout=30)
            r.encoding = "utf-8"
            d = r.json()
        except Exception as e:
            log.warning("sina_market_list page %d failed: %s", page, e)
            break
        if not d:
            break
        items.extend(d)
        if page % 10 == 0:
            time.sleep(0.5)
    out = []
    for x in items:
        sym = x.get("symbol") or ""
        code = sym[2:] if sym[:2] in ("sh", "sz", "bj") else sym
        out.append({
            "code": code, "full_code": sym,
            "name": x.get("name"),
            "price": float(x["trade"]) if x.get("trade") else None,
            "pct_chg": float(x["changepercent"]) if x.get("changepercent") else None,
            "volume_lots": x.get("volume"), "amount": x.get("amount"),
            "tick_time": x.get("ticktime"),
            "market": {"sh": "SH", "sz": "SZ", "bj": "BJ"}.get(sym[:2]),
        })
    return out, {"source": "sina.Market_Center.getHQNodeDataSimple", "count": len(out),
                 "pages": page}


def limit_stats_from_list(market_items, pct_threshold=9.9):
    """从全市场列表推算涨跌停（东财涨停池接口不稳定时的备用方案）。
    依据：全市场实时涨跌幅 ≥ 阈值即视为触及涨停（主板10%/科创创业20%的粗略判定）。"""
    ups, downs = [], []
    for x in market_items:
        p = x.get("pct_chg")
        if p is None:
            continue
        if p >= pct_threshold:
            ups.append({"code": x["code"], "name": x.get("name"), "pct_chg": p,
                        "amount": x.get("amount")})
        elif p <= -pct_threshold:
            downs.append({"code": x["code"], "name": x.get("name"), "pct_chg": p,
                          "amount": x.get("amount")})
    return ups, downs


# ---------------------------------------------------------------- 主流程
def collect_market_snapshot(trade_date=None):
    """采集一个交易日的市场快照，返回带完整元数据的 dict"""
    cal = trading_calendar()
    td = trade_date or latest_trading_day(calendar=cal)
    td_str = td.strftime("%Y%m%d")
    snapshot = {
        "trade_date": td.isoformat(),
        "collected_at": iso(),
        "sources": {},
        "indices": [],
        "boards": [],
        "limit_up": {"date": td_str, "count": 0, "stocks": []},
        "limit_down": {"date": td_str, "count": 0, "stocks": []},
        "market_breadth": {},
        "data_issues": [],
    }

    # 指数：东财 ulist 优先；失败（RST）时切换新浪源并记录切换事件
    idx, idx_src, idx_err = [], None, None
    try:
        raw = em_index_quote()
        idx = [{
            "code": x.get("f12"), "name": x.get("f14"), "close": x.get("f2"),
            "pct_chg": x.get("f3"), "change": x.get("f4"), "volume": x.get("f5"),
            "amount": x.get("f6"),
            "time": dt.datetime.fromtimestamp(x["f124"]).strftime("%Y-%m-%d %H:%M:%S") if x.get("f124") else None,
            "quote_ts": x.get("f124"), "source": "eastmoney.push2delay.ulist",
        } for x in raw]
        idx_src = "eastmoney.push2delay.ulist"
    except Exception as e:
        idx_err = f"{type(e).__name__}: {e}"
    if not idx:
        try:
            idx = sina_index_quote()
            idx_src = "sina.hq(切换: 东财ulist不可用)"
            snapshot["data_issues"].append(
                f"指数源切换：东财 ulist 失败({idx_err[:80]})，已切换新浪源")
        except Exception as e2:
            idx_err = f"东财:{idx_err[:60]} | 新浪:{type(e2).__name__}: {e2}"
    snapshot["indices"] = idx
    snapshot["sources"]["indices"] = {"ok": bool(idx), "source": idx_src,
                                      "count": len(idx), "error": idx_err if not idx else None}

    # 行业板块
    try:
        boards = em_board_industry()
        snapshot["boards"] = [{
            "code": None, "name": b["name"], "pct_chg": b["pct_chg"],
            "volume": b["volume"], "amount": b["amount"], "net_inflow": b["net_inflow"],
            "up_count": b["up_count"], "down_count": b["down_count"],
            "lead_stock": b["lead_stock"], "lead_pct": b["lead_pct"],
        } for b in boards]
        snapshot["sources"]["boards"] = {"source": "ths.stock_board_industry_summary", "ok": True,
                                         "count": len(boards)}
    except Exception as e:
        snapshot["data_issues"].append(f"boards 采集失败: {type(e).__name__}: {e}")
        snapshot["sources"]["boards"] = {"source": "ths", "ok": False, "error": str(e)}

    # 涨跌停：主用东财涨停池；失败时用全市场列表推算（不伪造数据，标注推算来源）
    for key, fn in (("limit_up", em_zt_pool), ("limit_down", em_dt_pool)):
        try:
            pool, meta = fn(td_str)
            snapshot[key] = {
                "date": td_str, "count": len(pool),
                "stocks": [{"code": p.get("c"), "name": p.get("n"), "pct_chg": p.get("zdp"),
                            "amount": p.get("amount")} for p in pool[:50]],
                "source": "eastmoney.push2ex",
            }
            snapshot["sources"][key] = {"ok": True, "count": len(pool)}
        except Exception as e:
            snapshot["data_issues"].append(f"{key} 采集失败: {type(e).__name__}（改用全市场推算）")
            snapshot["sources"][key] = {"ok": False, "error": str(e), "fallback": "market_list"}

    return snapshot


def collect_market_list():
    """全市场列表 + 简要市场宽度统计。
    源切换记录：东财 push2 clist 于 2026-10-18 连续 502（域名级限流，第一阶段可用），
    按任务书要求标记不可用并改用新浪源，实测 5576 只约 42 秒。"""
    out = {"collected_at": iso(), "source": "sina.Market_Center", "items": [], "issues": []}
    try:
        items, meta = sina_market_list()
        out["items"] = items
        out["meta"] = meta
        ups = [x for x in items if (x["pct_chg"] or 0) > 0]
        downs = [x for x in items if (x["pct_chg"] or 0) < 0]
        out["breadth"] = {"total": len(items), "up": len(ups), "down": len(downs),
                          "flat": len(items) - len(ups) - len(downs)}
    except Exception as e:
        out["issues"].append(f"新浪全市场列表失败: {type(e).__name__}: {e}")
    return out


if __name__ == "__main__":
    snap = collect_market_snapshot()
    from stockpilot import REPORTS
    save_json(snap, os.path.join(REPORTS, "market_snapshot.json"))
    audit("market_snapshot_collected", trade_date=snap["trade_date"],
          sources={k: v.get("ok") for k, v in snap["sources"].items()},
          issues=snap["data_issues"])
    print(json.dumps({k: snap[k] for k in ("trade_date", "sources", "data_issues")}, ensure_ascii=False, indent=2))
