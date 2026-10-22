"""直接 HTTP 层面的数据源探针：巨潮公告、上交所公告、东财数据中心、腾讯行情、新浪行情。
记录状态码/耗时/字节数/样例，落盘 evidence/http_probes.jsonl
"""
import json
import os
import time
import datetime as dt

import requests

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
EVID = os.path.join(BASE, "evidence")
OUT = os.path.join(EVID, "http_probes.jsonl")
UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0 Safari/537.36"
S = requests.Session()
S.headers.update({"User-Agent": UA})


def rec(name, resp, note="", sample=None):
    body = resp.content if resp is not None else b""
    r = {
        "probe": name,
        "at": dt.datetime.now().isoformat(timespec="seconds"),
        "status": getattr(resp, "status_code", None),
        "bytes": len(body),
        "encoding": getattr(resp, "encoding", None),
        "elapsed_s": round(getattr(resp, "elapsed", dt.timedelta()).total_seconds(), 2),
        "note": note,
        "sample": (sample if sample is not None else body[:300].decode("utf-8", "replace")),
    }
    with open(OUT, "a", encoding="utf-8") as f:
        f.write(json.dumps(r, ensure_ascii=False) + "\n")
    print(f"{r['status']} {r['bytes']:>8}B {r['elapsed_s']:5.2f}s {name} {note}")
    return r


def main():
    # 1. 巨潮资讯 公告查询（深市/沪市通用，按 stock 参数）
    try:
        r = S.post("https://www.cninfo.com.cn/new/hisAnnouncement/query",
                   data={"pageNum": 1, "pageSize": 5, "column": "szse", "tabName": "fulltext",
                         "stock": "000001,gssz0000001", "seDate": "2026-09-01~2026-10-17",
                         "isHLtitle": "true"},
                   headers={"Referer": "https://www.cninfo.com.cn/new/commonUrl?url=disclosure/list/notice",
                            "X-Requested-With": "XMLHttpRequest"}, timeout=20)
        j = r.json()
        rec("cninfo.hisAnnouncement sz000001", r,
            note=f"totalAnnouncement={j.get('totalAnnouncement')}",
            sample=json.dumps([{"t": a["announcementTitle"], "time": a["announcementTime"], "url": a["adjunctUrl"]}
                               for a in (j.get("announcements") or [])[:2]], ensure_ascii=False))
    except Exception as e:
        rec("cninfo.hisAnnouncement sz000001", None, note=f"ERROR {type(e).__name__}: {e}")

    # 2. 巨潮 沪市公告
    try:
        r = S.post("https://www.cninfo.com.cn/new/hisAnnouncement/query",
                   data={"pageNum": 1, "pageSize": 5, "column": "sse", "tabName": "fulltext",
                         "stock": "600519,gssh0600519", "seDate": "2026-09-01~2026-10-17",
                         "isHLtitle": "true"},
                   headers={"Referer": "https://www.cninfo.com.cn/", "X-Requested-With": "XMLHttpRequest"},
                   timeout=20)
        j = r.json()
        rec("cninfo.hisAnnouncement sh600519", r,
            note=f"totalAnnouncement={j.get('totalAnnouncement')}",
            sample=json.dumps([{"t": a["announcementTitle"], "time": a["announcementTime"]}
                               for a in (j.get("announcements") or [])[:2]], ensure_ascii=False))
    except Exception as e:
        rec("cninfo.hisAnnouncement sh600519", None, note=f"ERROR {type(e).__name__}: {e}")

    # 3. 上交所 公告（JSONP）
    try:
        r = S.get("https://query.sse.com.cn/security/stock/queryCompanyBulletinNew.do",
                  params={"jsonCallBack": "jsonpCallback", "productId": "600519",
                          "beginDate": "2026-09-01", "endDate": "2026-10-17",
                          "pageHelp.pageSize": 5, "pageHelp.pageNo": 1},
                  headers={"Referer": "https://www.sse.com.cn/"}, timeout=20)
        rec("sse.queryCompanyBulletinNew sh600519", r, note=f"len={len(r.text)}")
    except Exception as e:
        rec("sse.queryCompanyBulletinNew sh600519", None, note=f"ERROR {type(e).__name__}: {e}")

    # 4. 东财数据中心 财务主表
    try:
        r = S.get("https://datacenter-web.eastmoney.com/api/data/v1/get",
                  params={"reportName": "RPT_LICO_FN_CPD", "columns": "ALL",
                          "filter": '(SECURITY_CODE="600519")',
                          "pageNumber": 1, "pageSize": 2, "sortColumns": "REPORT_DATE",
                          "sortTypes": -1}, timeout=20)
        j = r.json()
        d = (j.get("result") or {}).get("data") or []
        rec("em.datacenter RPT_LICO_FN_CPD 600519", r,
            note=f"pages={((j.get('result') or {}).get('pages'))}",
            sample=json.dumps({k: d[0].get(k) for k in
                               ("SECURITY_NAME_ABBR", "REPORT_DATE", "NOTICE_DATE", "TOTAL_OPERATE_INCOME",
                                "PARENT_NETPROFIT", "WEIGHTAVG_ROE", "BASIC_EPS")} if d else {}, ensure_ascii=False))
    except Exception as e:
        rec("em.datacenter RPT_LICO_FN_CPD 600519", None, note=f"ERROR {type(e).__name__}: {e}")

    # 5. 东财 push2 实时快照（全市场）
    try:
        r = S.get("https://push2.eastmoney.com/api/qt/clist/get",
                  params={"pn": 1, "pz": 5, "po": 1, "np": 1, "fltt": 2, "invt": 2,
                          "fid": "f3", "fs": "m:0+t:6,m:0+t:80,m:1+t:2,m:1+t:23",
                          "fields": "f12,f14,f2,f3,f5,f6,f8,f20,f21"},
                  headers={"Referer": "https://quote.eastmoney.com/"}, timeout=20)
        rec("em.push2 clist 全市场快照", r, note="")
    except Exception as e:
        rec("em.push2 clist 全市场快照", None, note=f"ERROR {type(e).__name__}: {e}")

    # 6. 腾讯 日线（前复权）
    try:
        r = S.get("https://web.ifzq.gtimg.cn/appstock/app/fqkline/get",
                  params={"param": "sh600519,day,2026-09-01,2026-10-17,30,qfq"}, timeout=20)
        j = r.json()
        k = (j.get("data") or {}).get("sh600519", {}).get("qfqday") or []
        rec("tx.fqkline sh600519 qfq", r, note=f"bars={len(k)} last={k[-1] if k else None}")
    except Exception as e:
        rec("tx.fqkline sh600519 qfq", None, note=f"ERROR {type(e).__name__}: {e}")

    # 7. 新浪 实时（GBK）
    try:
        r = S.get("https://hq.sinajs.cn/list=sh600519,sz000001,sh000300",
                  headers={"Referer": "https://finance.sina.com.cn"}, timeout=20)
        txt = r.content.decode("gbk", "replace")
        rec("sina.hq realtime", r, note=f"len={len(txt)}", sample=txt[:220])
    except Exception as e:
        rec("sina.hq realtime", None, note=f"ERROR {type(e).__name__}: {e}")

    # 8. 东财 涨停池
    try:
        r = S.get("https://push2ex.eastmoney.com/getTopicZTPool",
                  params={"ut": "7eea3edcaed734bea9cbfc24409ed989", "dpt": "wz.ztzt",
                          "Pageindex": 0, "pagesize": 5, "sort": "fbt:asc", "date": "20261016"},
                  headers={"Referer": "https://quote.eastmoney.com/"}, timeout=20)
        j = r.json()
        rec("em.push2ex 涨停池 20261016", r,
            note=f"count={(j.get('data') or {}).get('total')}")
    except Exception as e:
        rec("em.push2ex 涨停池 20261016", None, note=f"ERROR {type(e).__name__}: {e}")


if __name__ == "__main__":
    main()
