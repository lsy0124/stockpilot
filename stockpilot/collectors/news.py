"""新闻与公告采集模块。

数据源（全部第一阶段实测通过）：
- 巨潮资讯 cninfo.com.cn（官方指定信息披露网站，公告主源，含原始 PDF 链接）
- 东财 np-anotice-stock（个股公告备源）
- 东财 stock_news_em via akshare（个股新闻）
- 央视新闻 news_cctv via akshare（宏观新闻锚点）

硬规则：
- 每条记录必须带：标题、原始来源链接、首次公开时间、系统发现时间、相关证券、数据可靠性
- 不编造无法获取的内容；不保存全文，只保存索引与必要摘要
- 区分来源类型（官方公告 / 媒体 / 机构观点），供下游做事实与观点分离
"""
import datetime as dt
import json
import os
import re
import subprocess

import requests

from .. import REPORTS, iso, save_json, setup_logger, audit

log = setup_logger("collector.news", "collector_news.log")

S = requests.Session()
S.headers.update({"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                                "AppleWebKit/537.36 Chrome/131.0 Safari/537.36"})

CNINFO_URL = "https://www.cninfo.com.cn/new/hisAnnouncement/query"
CNINFO_PDF = "https://static.cninfo.com.cn/"


# ---------------------------------------------------------------- 巨潮公告
def cninfo_announcements(sec_code=None, org_id=None, start=None, end=None, page_size=50):
    """巨潮公告查询。
    sec_code: 股票代码（如 600519）；org_id: 巨潮组织ID（沪市必需，如 gssh0600519）
    返回 (events, meta)
    """
    end = end or dt.date.today().isoformat()
    start = start or (dt.date.today() - dt.timedelta(days=14)).isoformat()
    data = {
        "pageNum": 1, "pageSize": page_size,
        "column": "szse" if (sec_code or "").startswith(("0", "3")) else "sse",
        "tabName": "fulltext",
        "seDate": f"{start}~{end}",
        "isHLtitle": "true",
    }
    if sec_code and org_id:
        data["stock"] = f"{sec_code},{org_id}"
    r = S.post(CNINFO_URL, data=data,
               headers={"Referer": "https://www.cninfo.com.cn/new/commonUrl?url=disclosure/list/notice",
                        "X-Requested-With": "XMLHttpRequest"}, timeout=25)
    j = r.json()
    items = j.get("announcements") or []
    events = []
    for a in items:
        ts = a.get("announcementTime")
        pub = dt.datetime.fromtimestamp(ts / 1000).isoformat() if ts else None
        events.append({
            "event_id": f"ann_{a.get('announcementId')}",
            "type": "announcement",
            "title": a.get("announcementTitle"),
            "sec_code": a.get("secCode"),
            "sec_name": a.get("secName"),
            "published_at": pub,
            "discovered_at": iso(),
            "source": "cninfo",
            "source_type": "official",        # 官方公告 → 事实
            "url": CNINFO_PDF + (a.get("adjunctUrl") or ""),
            "format": a.get("adjunctType"),
            "reliability": "high",
        })
    return events, {"source": "cninfo.hisAnnouncement", "total": j.get("totalAnnouncement"),
                    "count": len(events), "range": f"{start}~{end}"}


def cninfo_all_market(start=None, end=None, page_size=100):
    """全市场公告（不指定股票），用于发现重大事件"""
    end = end or dt.date.today().isoformat()
    start = start or (dt.date.today() - dt.timedelta(days=3)).isoformat()
    data = {"pageNum": 1, "pageSize": page_size, "column": "szse", "tabName": "fulltext",
            "seDate": f"{start}~{end}"}
    r = S.post(CNINFO_URL, data=data,
               headers={"Referer": "https://www.cninfo.com.cn/",
                        "X-Requested-With": "XMLHttpRequest"}, timeout=25)
    j = r.json()
    items = j.get("announcements") or []
    events = []
    for a in items:
        ts = a.get("announcementTime")
        pub = dt.datetime.fromtimestamp(ts / 1000).isoformat() if ts else None
        events.append({
            "event_id": f"ann_{a.get('announcementId')}",
            "type": "announcement",
            "title": a.get("announcementTitle"),
            "sec_code": a.get("secCode"),
            "sec_name": a.get("secName"),
            "published_at": pub,
            "discovered_at": iso(),
            "source": "cninfo",
            "source_type": "official",
            "url": CNINFO_PDF + (a.get("adjunctUrl") or ""),
            "reliability": "high",
        })
    return events, {"source": "cninfo.hisAnnouncement(all)", "total": j.get("totalAnnouncement"),
                    "count": len(events)}


# ---------------------------------------------------------------- 东财个股公告
def em_stock_announcements(sec_code, page_size=20):
    """东财个股公告（备源）"""
    url = "https://np-anotice-stock.eastmoney.com/api/security/ann"
    params = {"sr": -1, "page_size": page_size, "page_index": 1, "ann_type": "A",
              "client_source": "web", "stock_list": sec_code, "f_node": 0, "s_node": 0}
    r = S.get(url, params=params, timeout=25)
    j = r.json()
    items = (((j.get("data") or {}).get("list")) or [])
    events = []
    for it in items:
        events.append({
            "event_id": f"emann_{it.get('art_code')}",
            "type": "announcement",
            "title": it.get("title"),
            "sec_code": sec_code,
            "published_at": it.get("notice_date"),
            "discovered_at": iso(),
            "source": "eastmoney.np-anotice",
            "source_type": "official",
            "url": it.get("art_url"),
            "reliability": "high",
        })
    return events, {"source": "eastmoney.np-anotice-stock", "count": len(events)}


# ---------------------------------------------------------------- 个股新闻
def em_stock_news(symbol, limit=10):
    """东财个股新闻（媒体来源）。

    实现说明：东财 search-api-web 接口对 curl 的小写 percent-encoding 正常解析，
    但对 Python requests/urllib 发送的编码请求只返回 1 条（服务端解析怪癖，
    已实测确认：URL 完全相同、UA 换成 curl 也仍返回 1 条，仅 curl 命令行成功）。
    akshare 的 stock_news_em 则因 pyarrow 正则兼容 bug 直接抛异常。
    故此处经 subprocess 调用本机 curl 取数，并在证据文件中记录该依赖。
    """
    import subprocess
    inner = {"uid": "", "keyword": symbol, "type": ["cmsArticleWebOld"], "client": "web",
             "clientType": "web", "clientVersion": "curr",
             "param": {"cmsArticleWebOld": {"searchScope": "default", "sort": "default",
                                            "pageIndex": 1, "pageSize": limit,
                                            "preTag": "<em>", "postTag": "</em>"}}}
    param = json.dumps(inner, ensure_ascii=False, separators=(",", ":"))
    cmd = ["curl", "-s", "--max-time", "25", "-G",
           "https://search-api-web.eastmoney.com/search/jsonp",
           "--data-urlencode", "cb=cb", "--data-urlencode", f"param={param}",
           "-H", "Referer: https://so.eastmoney.com/"]
    try:
        out = subprocess.run(cmd, capture_output=True, timeout=35).stdout.decode("utf-8", "replace")
    except Exception as e:
        log.warning("em_stock_news curl failed %s: %s", symbol, e)
        return [], {"source": "eastmoney.search", "ok": False, "error": str(e)}
    text = out.strip()
    i, k = text.find("("), text.rfind(")")
    if i < 0 or k < 0:
        return [], {"source": "eastmoney.search", "ok": False, "error": "JSONP parse failed"}
    j = json.loads(text[i + 1:k])
    items = j["result"].get("cmsArticleWebOld") or []
    events = []
    for it in items:
        events.append({
            "event_id": f"news_{it.get('code') or symbol}_{hash(it.get('title')) & 0xffff:04x}",
            "type": "news",
            "title": re.sub("<[^>]+>", "", it.get("title") or ""),
            "summary": re.sub("<[^>]+>", "", (it.get("content") or ""))[:300],
            "sec_code": symbol,
            "published_at": it.get("date"),
            "discovered_at": iso(),
            "source": it.get("mediaName"),
            "source_url": it.get("url"),
            "source_type": "media",          # 媒体 → 观点/转述
            "reliability": "medium",
        })
    return events, {"source": "eastmoney.search(jsonp via curl)", "count": len(events),
                    "hits_total": j.get("hitsTotal")}


# ---------------------------------------------------------------- 宏观新闻
def cctv_news(date=None, lookback=3):
    """央视新闻联播（宏观政策锚点）。周末/节假日无数据时向前回溯。"""
    import akshare as ak
    d = date or dt.date.today().strftime("%Y%m%d")
    for i in range(lookback + 1):
        try_d = (dt.datetime.strptime(d, "%Y%m%d") - dt.timedelta(days=i)).strftime("%Y%m%d")
        try:
            df = ak.news_cctv(date=try_d)
            if len(df):
                events = []
                for _, r in df.iterrows():
                    events.append({
                        "event_id": f"cctv_{try_d}_{hash(r.get('title')) & 0xffffffff:08x}",
                        "type": "macro_news",
                        "title": r.get("title"),
                        "summary": (r.get("content") or "")[:300],
                        "published_at": try_d,
                        "discovered_at": iso(),
                        "source": "央视新闻联播",
                        "source_url": "https://news.cctv.com/",
                        "source_type": "state_media",    # 官方媒体
                        "reliability": "high",
                    })
                return events, {"source": "cctv.news", "date_used": try_d, "requested": d,
                                "count": len(events)}
        except Exception as e:
            log.warning("news_cctv %s failed: %s", try_d, e)
    return [], {"source": "cctv.news", "ok": False, "error": f"最近{lookback}天无数据"}


# ---------------------------------------------------------------- 去重与整理
def dedup(events):
    """按 标题规范化 + 来源去重；同一事件的不同转载只保留最早发现的一条"""
    seen = {}
    for e in events:
        key = re.sub(r"\s+", "", (e.get("title") or ""))[:60]
        if not key:
            continue
        if key not in seen or (e.get("published_at") or "") < (seen[key].get("published_at") or "z"):
            if key in seen and e.get("source_type") == "official":
                pass
            seen[key] = e
    return list(seen.values())


def collect_news(watch_codes=None, days=14):
    """新闻与公告主流程"""
    end = dt.date.today().isoformat()
    start = (dt.date.today() - dt.timedelta(days=days)).isoformat()
    out = {
        "collected_at": iso(),
        "window": {"start": start, "end": end},
        "events": [],
        "sources": {},
        "issues": [],
    }

    # 1. 全市场公告（深市）
    try:
        ev, meta = cninfo_all_market(start=start, end=end, page_size=60)
        out["events"].extend(ev)
        out["sources"]["cninfo_all"] = meta
    except Exception as e:
        out["issues"].append(f"cninfo_all 失败: {type(e).__name__}: {e}")

    # 2. 重点股票公告
    for code, org in (watch_codes or []):
        try:
            ev, meta = cninfo_announcements(sec_code=code, org_id=org, start=start, end=end)
            out["events"].extend(ev)
            out["sources"][f"cninfo_{code}"] = meta
        except Exception as e:
            out["issues"].append(f"cninfo_{code} 失败: {type(e).__name__}: {e}")

    # 3. 宏观新闻
    try:
        ev, meta = cctv_news()
        out["events"].extend(ev)
        out["sources"]["cctv"] = meta
    except Exception as e:
        out["issues"].append(f"cctv 失败: {type(e).__name__}: {e}")

    # 4. 重点股票新闻（媒体）
    for code in [c for c, _ in (watch_codes or [])][:5]:
        try:
            ev, meta = em_stock_news(code)
            out["events"].extend(ev)
            out["sources"][f"news_{code}"] = meta
        except Exception as e:
            out["issues"].append(f"news_{code} 失败: {type(e).__name__}: {e}")

    before = len(out["events"])
    out["events"] = dedup(out["events"])
    out["dedup"] = {"before": before, "after": len(out["events"])}
    return out


if __name__ == "__main__":
    res = collect_news(watch_codes=[("600519", "gssh0600519"), ("000001", "gssz0000001")])
    save_json(res, os.path.join(REPORTS, "news_events.json"))
    audit("news_collected", count=len(res["events"]), dedup=res.get("dedup"), issues=res["issues"])
    print("events:", len(res["events"]), "dedup:", res.get("dedup"), "issues:", res["issues"])
