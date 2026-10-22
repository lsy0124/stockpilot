"""数据源健康登记表（第四阶段新增，替代简版 source_health）。

任务书 4.1 要求每个数据源登记十个字段：
  名称 / 具体接口 / 可获取数据类型 / 最近成功时间 / 最近失败时间 /
  失败原因 / 请求成功率 / 数据延迟 / 可用备用来源 / 合规使用限制

设计：
- SourceRegistry：静态登记表（数据源元数据，人工维护）
- 运行时统计：从 data/source_stats.json 累积调用记录，计算成功率与延迟
- 合并视图：registry（静态）+ stats（动态）→ health_report.json
- 持续失败的源标记降级/停用建议，不无限重试
"""
import datetime as dt
import json
import os
import time

from stockpilot import DATA, audit, iso, load_json, save_json, setup_logger

log = setup_logger("source_registry", "source_registry.log")

STATS_FILE = os.path.join(DATA, "source_stats.json")
HEALTH_FILE = os.path.join(DATA, "source_health_report.json")

# 静态登记表：所有已被本项目使用过的数据源（含已停用的）
REGISTRY = [
    {
        "name": "腾讯行情",
        "endpoint": "web.ifzq.gtimg.cn/appstock/app/fqkline/get",
        "data_types": ["日线(前复权)", "当日分时"],
        "status": "degraded",
        "backup": "新浪 CN_MarketDataService.getKLineData（日线，不复权）",
        "limits": "批量连续请求触发 WAF 501；需 ≥0.35s 间隔；不得伪造指纹或轮换代理",
        "notes": "2026-10-18 实测：连续请求后 WAF 封锁，休息 60 秒不解除。已停止用于趋势批量取数。",
    },
    {
        "name": "新浪行情-实时",
        "endpoint": "hq.sinajs.cn/list=",
        "data_types": ["实时快照(价/量/额)", "主要指数"],
        "status": "active",
        "backup": "东财 push2delay ulist（当前 RST 不可用）",
        "limits": "GBK 编码需 Referer 头；指数接口不返回行情时刻（已标注采集时间近似）",
        "notes": "三方价格交叉校验主源之一，实测与腾讯一致（0 分歧）。",
    },
    {
        "name": "新浪行情-日线",
        "endpoint": "quotes.sina.cn/cn/api/json_v2.php/CN_MarketDataService.getKLineData",
        "data_types": ["日线(不复权)"],
        "status": "active",
        "backup": "腾讯 fqkline（WAF 受限）、akshare stock_zh_a_daily（约5s/次，不适合批量）",
        "limits": "不复权价，除权日跳空由质量检查标记；实测 40 次连续请求 0 失败",
        "notes": "当前趋势计算主力源。",
    },
    {
        "name": "新浪行情-全市场列表",
        "endpoint": "vip.stock.finance.sina.com.cn/.../Market_Center.getHQNodeDataSimple",
        "data_types": ["全市场代码/名称/价/涨跌幅/成交量/成交额"],
        "status": "active",
        "backup": "东财 push2 clist（持续 502 不可用）",
        "limits": "分页 100 只/页，共约 56 页、42-47 秒；不宜高频调用",
        "notes": "东财 clist 停用后的唯一全市场来源，存在单源集中风险。",
    },
    {
        "name": "东财-指数",
        "endpoint": "push2delay.eastmoney.com/api/qt/ulist.np/get",
        "data_types": ["主要指数实时(带时间戳)"],
        "status": "down",
        "backup": "新浪 hq.sinajs.cn 指数（已切换为主力）",
        "limits": "2026-10-18 起 RST（RemoteDisconnected）；不得伪造指纹绕过",
        "notes": "东财第三个不可用接口。采集器已实现自动切换并记录切换原因。",
    },
    {
        "name": "东财-全市场列表",
        "endpoint": "push2.eastmoney.com/api/qt/clist/get",
        "data_types": ["全市场快照"],
        "status": "down",
        "backup": "新浪 Market_Center（已切换为主力）",
        "limits": "第二阶段起持续 502（域名级限流）",
        "notes": "已停用。",
    },
    {
        "name": "东财-历史K线",
        "endpoint": "push2his.eastmoney.com/api/qt/stock/kline/get",
        "data_types": ["日/分钟线"],
        "status": "down",
        "backup": "腾讯 fqkline（WAF）、新浪日线（不复权）",
        "limits": "第一阶段末起持续 RST",
        "notes": "已停用。",
    },
    {
        "name": "东财-涨停跌停池",
        "endpoint": "push2ex.eastmoney.com/getTopicZTPool",
        "data_types": ["涨停池/跌停池"],
        "status": "degraded",
        "backup": "全市场列表涨跌幅推算（≥9.9% 判涨停，标注 derived）",
        "limits": "间歇超时；失败时降级推算并标记来源",
        "notes": "推算值与官方池可能有误差（新股/科创创业阈值不同）。",
    },
    {
        "name": "东财-行业板块",
        "endpoint": "同花顺 stock_board_industry_summary_ths",
        "data_types": ["90 个行业涨跌幅/涨跌家数/领涨股/净流入"],
        "status": "active",
        "backup": "东财 push2 clist m:90 t:2（502 不可用）",
        "limits": "经 akshare 调用，依赖 akshare 版本兼容",
        "notes": "东财板块接口停用后切换同花顺源。",
    },
    {
        "name": "巨潮资讯-公告",
        "endpoint": "www.cninfo.com.cn/new/hisAnnouncement/query",
        "data_types": ["上市公司公告(官方披露)", "原始 PDF 链接"],
        "status": "active",
        "backup": "东财 np-anotice-stock（个股公告备源）",
        "limits": "沪市需 orgId 参数；深市用 column=szse",
        "notes": "官方指定信息披露网站，事实类数据主源。",
    },
    {
        "name": "东财-个股新闻",
        "endpoint": "search-api-web.eastmoney.com/search/jsonp",
        "data_types": ["个股新闻(媒体)"],
        "status": "active",
        "backup": "无（媒体观点类，缺失不影响事实链）",
        "limits": "必须经 curl 命令行调用（requests 的 URL 编码被服务端拒绝，"
                  "返回 hitsTotal=1 的怪癖）；akshare stock_news_em 有 pyarrow 正则 bug",
        "notes": "媒体来源，属观点/转述，与官方公告区分存档。",
    },
    {
        "name": "央视新闻",
        "endpoint": "akshare news_cctv（tv.cctv.com）",
        "data_types": ["新闻联播(宏观政策)"],
        "status": "degraded",
        "backup": "无（宏观锚点，缺失时在报告中披露）",
        "limits": "周末无数据；tv.cctv.com 出现过连接超时，已加 3 天回溯",
        "notes": "官方媒体，宏观政策锚点。",
    },
    {
        "name": "akshare 聚合层",
        "endpoint": "akshare 库",
        "data_types": ["财报摘要", "业绩报表", "交易日历", "板块(同花顺)"],
        "status": "active",
        "backup": "各接口的直连 HTTP 实现",
        "limits": "需 pandas<3（pyarrow 正则兼容 bug）；部分接口慢（约5s/次）",
        "notes": "财报与业绩报表的唯一来源，暂未做多源核验。",
    },
]

# 每个数据源的运行时 key（用于 stats 累积）
STATS_KEYS = {
    "腾讯行情": "tencent.fqkline",
    "新浪行情-实时": "sina.hq",
    "新浪行情-日线": "sina.kline",
    "新浪行情-全市场列表": "sina.market_list",
    "东财-指数": "eastmoney.ulist",
    "东财-全市场列表": "eastmoney.clist",
    "东财-历史K线": "eastmoney.push2his",
    "东财-涨停跌停池": "eastmoney.ztpool",
    "东财-行业板块": "ths.boards",
    "巨潮资讯-公告": "cninfo.announcement",
    "东财-个股新闻": "eastmoney.news",
    "央视新闻": "cctv.news",
    "akshare 聚合层": "akshare",
}


def _load_stats():
    if os.path.exists(STATS_FILE):
        try:
            return load_json(STATS_FILE)
        except Exception:
            return {}
    return {}


def record_call(source_name, ok, elapsed_s=None, error=None):
    """记录一次调用（供成功率与延迟统计）。source_name 用登记表中文名。"""
    key = STATS_KEYS.get(source_name, source_name)
    stats = _load_stats()
    s = stats.setdefault(key, {"calls": 0, "ok": 0, "fail": 0, "latencies": [],
                               "last_ok_at": None, "last_fail_at": None,
                               "last_error": None})
    s["calls"] += 1
    if ok:
        s["ok"] += 1
        s["last_ok_at"] = iso()
        if elapsed_s is not None:
            s["latencies"] = (s["latencies"] + [round(elapsed_s, 2)])[-50:]
    else:
        s["fail"] += 1
        s["last_fail_at"] = iso()
        s["last_error"] = str(error)[:200] if error else None
    save_json(stats, STATS_FILE)
    return s


def build_health_report():
    """合并静态登记表与运行时统计，输出完整健康报告"""
    stats = _load_stats()
    sources = []
    for entry in REGISTRY:
        key = STATS_KEYS.get(entry["name"], entry["name"])
        st = stats.get(key, {})
        calls = st.get("calls", 0)
        ok = st.get("ok", 0)
        lat = sorted(st.get("latencies", []))
        p50 = lat[len(lat) // 2] if lat else None
        p95 = lat[int(len(lat) * 0.95)] if lat else None
        # 连续失败判定（用于降级建议）
        consecutive_fail = 0
        # 简化：用 last_fail_at > last_ok_at 且 fail>=3 判定
        down_suggestion = None
        if st.get("fail", 0) >= 3 and (st.get("last_ok_at") or "") < (st.get("last_fail_at") or ""):
            down_suggestion = "连续失败且无成功恢复，建议停用并切换备用源"
        elif entry["status"] == "down":
            down_suggestion = "已确认不可用（RST/502），使用备用源"
        sources.append({
            **entry,
            "stats_key": key,
            "calls_total": calls,
            "calls_ok": ok,
            "calls_fail": st.get("fail", 0),
            "success_rate_pct": round(ok / calls * 100, 1) if calls else None,
            "last_ok_at": st.get("last_ok_at"),
            "last_fail_at": st.get("last_fail_at"),
            "last_error": st.get("last_error"),
            "latency_p50_s": p50,
            "latency_p95_s": p95,
            "recommendation": down_suggestion or "正常使用",
        })
    report = {
        "generated_at": iso(),
        "sources": sources,
        "summary": {
            "total": len(sources),
            "active": sum(1 for s in sources if s["status"] == "active"),
            "degraded": sum(1 for s in sources if s["status"] == "degraded"),
            "down": sum(1 for s in sources if s["status"] == "down"),
            "single_source_risks": [
                s["name"] for s in sources
                if s["status"] == "active" and ("无（" in (s.get("backup") or "") or s.get("backup", "").startswith("无"))
            ],
        },
        "compliance_note": "所有数据源均未使用伪造设备指纹、代理轮换或 WAF 规避手段；"
                           "受限接口一律停用或降级并记录。",
    }
    save_json(report, HEALTH_FILE)
    audit("source_health_report_built", summary=report["summary"])
    return report


if __name__ == "__main__":
    rep = build_health_report()
    print(json.dumps(rep["summary"], ensure_ascii=False, indent=1))
    for s in rep["sources"]:
        print(f"{s['name']:16s} {s['status']:9s} 成功率={s['success_rate_pct']} "
              f"延迟p50={s['latency_p50_s']}s 建议={s['recommendation']}")
