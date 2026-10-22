"""补充探针：深交所公告分板块（沪市 stock 参数需要 orgId）、东财公告列表、腾讯分钟线、百度股市通。"""
import datetime as dt
import json
import os
import requests

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
EVID = os.path.join(BASE, "evidence")
OUT = os.path.join(EVID, "http_probes.jsonl")
UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/131.0 Safari/537.36"
S = requests.Session()
S.headers.update({"User-Agent": UA})


def rec(name, resp, note="", sample=None):
    elapsed = getattr(resp, "elapsed", None)
    body = resp.content if resp is not None else b""
    r = {"probe": name, "status": getattr(resp, "status_code", None), "bytes": len(body),
         "elapsed_s": round(elapsed.total_seconds(), 2) if isinstance(elapsed, dt.timedelta) else None,
         "note": note,
         "sample": sample if sample is not None else body[:280].decode("utf-8", "replace")}
    with open(OUT, "a", encoding="utf-8") as f:
        f.write(json.dumps(r, ensure_ascii=False) + "\n")
    el = r['elapsed_s']
    print(f"{r['status']} {r['bytes']:>8}B {el if el is not None else -1:5.2f}s {name} {note}")
    return r


def main():
    # 1. 巨潮：按 orgId 查沪市（茅台）— 先从东财获取 orgId
    try:
        r = S.get("https://push2.eastmoney.com/api/qt/stock/get",
                  params={"secid": "1.600519", "fields": "f57,f58,f84,f92,f107",
                          "invt": 2, "fltt": 2},
                  headers={"Referer": "https://quote.eastmoney.com/"}, timeout=15)
        j = r.json()
        d = j.get("data") or {}
        rec("em.push2 stock/get 600519 元信息", r,
            note=f"name={d.get('f58')} 总股本={d.get('f84')} 流通={d.get('f84')}", sample="")
    except Exception as e:
        rec("em.push2 stock/get 600519", None, note=f"ERROR {e}")

    # 2. 巨潮：全市场公告（不指定 stock，按 column=szse）
    try:
        r = S.post("https://www.cninfo.com.cn/new/hisAnnouncement/query",
                   data={"pageNum": 1, "pageSize": 3, "column": "szse", "tabName": "fulltext",
                         "seDate": "2026-10-15~2026-10-17"},
                   headers={"Referer": "https://www.cninfo.com.cn/", "X-Requested-With": "XMLHttpRequest"},
                   timeout=20)
        j = r.json()
        rec("cninfo.hisAnnouncement szse 全市场", r, note=f"total={j.get('totalAnnouncement')}",
            sample=json.dumps([{"c": a["secCode"], "t": a["announcementTitle"], "time": a["announcementTime"]}
                               for a in (j.get("announcements") or [])[:3]], ensure_ascii=False))
    except Exception as e:
        rec("cninfo.hisAnnouncement szse 全市场", None, note=f"ERROR {e}")

    # 3. 腾讯 5 分钟线
    try:
        r = S.get("https://web.ifzq.gtimg.cn/appstock/app/kline/mkline",
                  params={"param": "sh600519,m5,,10"}, timeout=20)
        j = r.json()
        k = ((j.get("data") or {}).get("sh600519") or {}).get("m5k") or \
            ((j.get("data") or {}).get("sh600519") or {}).get("qfqday") or []
        rec("tx.mkline sh600519 m5", r, note=f"bars={len(k)}", sample=json.dumps(k[:3], ensure_ascii=False))
    except Exception as e:
        rec("tx.mkline sh600519 m5", None, note=f"ERROR {e}")

    # 4. 腾讯 分时（今日）
    try:
        r = S.get("https://web.ifzq.gtimg.cn/appstock/app/minute/query",
                  params={"code": "sh600519"}, timeout=20)
        j = r.json()
        data = ((j.get("data") or {}).get("sh600519") or {}).get("data") or {}
        rec("tx.minute sh600519", r, note=f"pts={len(data.get('data') or [])}",
            sample=json.dumps((data.get("data") or [])[:3]))
    except Exception as e:
        rec("tx.minute sh600519", None, note=f"ERROR {e}")

    # 5. 东财 公告列表（个股）
    try:
        r = S.get("https://np-anotice-stock.eastmoney.com/api/security/ann",
                  params={"sr": -1, "page_size": 5, "page_index": 1, "ann_type": "A",
                          "client_source": "web", "stock_list": "600519", "f_node": 0,
                          "s_node": 0}, timeout=20)
        j = r.json()
        items = (((j.get("data") or {}).get("list")) or [])
        rec("em.np-anotice-stock 600519", r, note=f"count={len(items)}",
            sample=json.dumps([{"t": i.get("title"), "d": i.get("notice_date"), "url": i.get("art_url")}
                               for i in items[:3]], ensure_ascii=False))
    except Exception as e:
        rec("em.np-anotice-stock 600519", None, note=f"ERROR {e}")

    # 6. 百度股市通 实时（备用）
    try:
        r = S.get("https://finance.pae.baidu.com/selfselect/getstockinfo",
                  params={"code": "600519", "market": "ab", "type": "ns", "finClientType": "pc"},
                  timeout=15)
        rec("baidu.getstockinfo 600519", r, note="")
    except Exception as e:
        rec("baidu.getstockinfo 600519", None, note=f"ERROR {e}")

    # 7. 雪球 实时（备用）
    try:
        r = S.get("https://stock.xueqiu.com/v5/stock/quote.json",
                  params={"symbol": "SH600519", "extend": "detail"}, timeout=15)
        rec("xueqiu.quote SH600519", r, note="")
    except Exception as e:
        rec("xueqiu.quote SH600519", None, note=f"ERROR {e}")

    # 8. 深交所 公告（备用）
    try:
        r = S.get("https://www.szse.cn/api/disc/announcement/annList",
                  params={"random": "0.1", "pageSize": 3, "pageNum": 1, "channelCode": ["listedNotice"],
                          "seDate": ["2026-10-15", "2026-10-17"], "stock": ["000001"]},
                  headers={"Referer": "https://www.szse.cn/disclosure/listed/notice/index.html"}, timeout=20)
        rec("szse.annList 000001", r, note="")
    except Exception as e:
        rec("szse.annList 000001", None, note=f"ERROR {e}")

    # 9. 中证/上交所 指数行情（备用）
    try:
        r = S.get("https://query.sse.com.cn/commonQuery.do",
                  params={"sqlId": "COMMON_SSE_SJ_SCZS_C", "type": "sczs", "zqdm": "000300"},
                  headers={"Referer": "https://www.sse.com.cn/"}, timeout=15)
        rec("sse.commonQuery 000300 指数", r, note="")
    except Exception as e:
        rec("sse.commonQuery 000300", None, note=f"ERROR {e}")


if __name__ == "__main__":
    main()
