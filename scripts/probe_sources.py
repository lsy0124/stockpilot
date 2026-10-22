"""StockPilot 阶段A 数据源探针。
每次调用记录: 源名称/函数/参数/耗时/行数/列/前3行/异常, 结果落盘 evidence/probes.jsonl
"""
import json
import os
import sys
import time
import traceback
import datetime as dt

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
EVID = os.path.join(BASE, "evidence")
os.makedirs(EVID, exist_ok=True)
OUT = os.path.join(EVID, "probes.jsonl")


def probe(name, fn, *args, **kwargs):
    t0 = time.time()
    rec = {"probe": name, "started": dt.datetime.now().isoformat(timespec="seconds")}
    try:
        df = fn(*args, **kwargs)
        rec["ok"] = True
        rec["elapsed_s"] = round(time.time() - t0, 2)
        try:
            rec["rows"] = int(len(df))
            rec["cols"] = [str(c) for c in list(df.columns)]
            rec["head"] = json.loads(df.head(3).to_json(orient="records", force_ascii=False, date_format="iso"))
            rec["tail"] = json.loads(df.tail(2).to_json(orient="records", force_ascii=False, date_format="iso"))
            rec["dtypes"] = {str(k): str(v) for k, v in df.dtypes.items()}
        except Exception:
            rec["repr"] = repr(df)[:2000]
    except Exception as e:
        rec["ok"] = False
        rec["elapsed_s"] = round(time.time() - t0, 2)
        rec["error"] = f"{type(e).__name__}: {e}"
        rec["trace"] = traceback.format_exc()[-1500:]
    with open(OUT, "a", encoding="utf-8") as f:
        f.write(json.dumps(rec, ensure_ascii=False) + "\n")
    print(("OK  " if rec["ok"] else "FAIL") + f" {name:38s} {rec['elapsed_s']:6.2f}s "
          + (f"rows={rec.get('rows')}" if rec["ok"] else rec.get("error", "")[:120]))
    return rec


def main():
    import akshare as ak
    only = sys.argv[1] if len(sys.argv) > 1 else "all"

    def want(k):
        return only == "all" or only == k

    if want("daily"):
        probe("ak.stock_zh_a_hist 600519 daily", ak.stock_zh_a_hist,
              symbol="600519", period="daily", start_date="20250101", end_date="20261017", adjust="qfq")
        probe("ak.stock_zh_a_hist 000001 daily noadj", ak.stock_zh_a_hist,
              symbol="000001", period="daily", start_date="20250101", end_date="20261017", adjust="")
        probe("ak.stock_zh_a_hist 510300 ETF daily", ak.stock_zh_a_hist,
              symbol="510300", period="daily", start_date="20250101", end_date="20261017", adjust="qfq")

    if want("spot"):
        probe("ak.stock_zh_a_spot_em full snapshot", ak.stock_zh_a_spot_em)
        probe("ak.fund_etf_spot_em ETF snapshot", ak.fund_etf_spot_em)

    if want("minute"):
        probe("ak.stock_zh_a_hist_min_em 5min", ak.stock_zh_a_hist_min_em,
              symbol="600519", period="5", adjust="qfq")

    if want("calendar"):
        probe("ak.tool_trade_date_hist_sina calendar", ak.tool_trade_date_hist_sina)

    if want("news"):
        probe("ak.stock_news_em 600519", ak.stock_news_em, symbol="600519")
        probe("ak.news_cctv 20261016", ak.news_cctv, date="20261016")

    if want("ann"):
        probe("ak.stock_notice_report 600519", ak.stock_notice_report,
              symbol="600519", date="20261016")

    if want("fin"):
        probe("ak.stock_financial_abstract 600519", ak.stock_financial_abstract, symbol="600519")
        probe("ak.stock_financial_report_sina 利润表", ak.stock_financial_report_sina,
              stock="sh600519", symbol="利润表")
        probe("ak.stock_yjbb_em 业绩报表 20260630", ak.stock_yjbb_em, date="20260630")
        probe("ak.stock_zh_a_gdhs 股东户数", ak.stock_zh_a_gdhs, symbol="600519")

    if want("index"):
        probe("ak.stock_zh_index_daily sh000001", ak.stock_zh_index_daily, symbol="sh000001")
        probe("ak.stock_board_industry_name_em 板块", ak.stock_board_industry_name_em)

    if want("limit"):
        probe("ak.stock_zt_pool_em 涨停池 20261016", ak.stock_zt_pool_em, date="20261016")
        probe("ak.stock_zh_a_stop_em 停牌 20261016", ak.stock_zh_a_stop_em, date="20261016")


if __name__ == "__main__":
    main()
