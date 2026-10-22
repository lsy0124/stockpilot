"""StockPilot 第四阶段端到端流水线。

完整闭环（任务书第三章）：
  采集真实数据 → 校验质量 → 生成候选 → 智谱独立研究 → 反方审查
  → GPT 研究交接 → （用户确认模拟交易 → 账本记录 → 历史复盘）

在 v3 基础上新增：
- 数据源健康登记与调用统计（source_registry）
- 跨数据源核验（crosscheck）
- 1000 元可交易性分层筛选（tradeability）
- 财报维度接入（research.fetch_fundamentals）
- 反方审查 9 项检查（含数据冲突/历史失败/财务质量）
- v4 交接包（10 文件 + manifest + ZIP）

用法：
    python -m stockpilot.pipeline_v4              # 完整流程
    python -m stockpilot.pipeline_v4 --skip-scan  # 复用全市场列表
"""
import json
import os
import sys
import time

from stockpilot import REPORTS, audit, iso, load_json, save_json, setup_logger
from stockpilot import ledger as L
from stockpilot import report as report_mod
from stockpilot import review as review_mod
from stockpilot import source_health as sh
from stockpilot import source_registry as sr
from stockpilot import timeutil as tu
from stockpilot import vault_sync
from stockpilot import crosscheck, tradeability
from stockpilot.collectors import news as news_col
from stockpilot.collectors import quote as q
from stockpilot import quality, screener
from stockpilot.research import build_adversarial, build_research, fetch_fundamentals
from stockpilot import handoff_v4

log = setup_logger("pipeline_v4", "pipeline_v4.log")

WATCH = [("600519", "gssh0600519"), ("000001", "gssz0000001"), ("600036", "gssh0600036")]
HEALTH = sh.SourceHealth()


def step(name, fn, *args, **kwargs):
    t0 = time.time()
    log.info("STEP %s 开始", name)
    try:
        res = fn(*args, **kwargs)
        log.info("STEP %s 完成 耗时 %.1fs", name, time.time() - t0)
        audit("pipeline_step", step=name, ok=True, elapsed=round(time.time() - t0, 1),
              pipeline="v4")
        return res
    except Exception as e:
        import traceback
        log.error("STEP %s 失败: %s", name, e)
        audit("pipeline_step", step=name, ok=False, error=f"{type(e).__name__}: {e}",
              trace=traceback.format_exc()[-600:], pipeline="v4")
        return None


def timed_source(source_name, fn, *args, **kwargs):
    """带数据源统计的调用包装"""
    t0 = time.time()
    try:
        res = fn(*args, **kwargs)
        sr.record_call(source_name, True, elapsed_s=time.time() - t0)
        return res
    except Exception as e:
        sr.record_call(source_name, False, error=e)
        raise


def main(skip_scan=False):
    run_id = tu.now().strftime("%Y%m%d_%H%M%S")
    log.info("===== StockPilot 第四阶段流水线启动 run_id=%s =====", run_id)
    audit("pipeline_start", run_id=run_id, pipeline="v4", skip_scan=skip_scan)
    ctx = {}

    # 0. 账本自愈 + 数据源健康报告
    def ledger_setup():
        L.init_db()
        rec = L.reconcile()
        integ = L.integrity_check()
        if not rec["ok"]:
            audit("ledger_reconcile_failed", detail=rec)
        return {"reconcile": rec, "integrity": integ}
    ctx["ledger"] = step("ledger_check", ledger_setup) or {}
    ctx["source_health"] = step("source_health", sr.build_health_report) or {}

    # 1. 行情快照
    snap = step("market_snapshot", timed_source, "新浪行情-实时",
                q.collect_market_snapshot)
    ctx["snapshot"] = snap or {}

    # 2. 全市场列表
    if skip_scan and os.path.exists(REPORTS + "/market_list.json"):
        ml = load_json(REPORTS + "/market_list.json")
        log.info("复用已有全市场列表（--skip-scan）")
    else:
        def _ml():
            return timed_source("新浪行情-全市场列表", q.collect_market_list)
        ml = step("market_list", _ml)
    ctx["market_list"] = ml or {"items": [], "issues": ["未采集"]}

    # 2b. 涨停池备用推算
    if ctx["market_list"].get("items"):
        lu = ctx["snapshot"].get("limit_up") or {}
        if not lu.get("count"):
            ups, downs = q.limit_stats_from_list(ctx["market_list"]["items"])
            ctx["snapshot"]["limit_up"] = {"date": ctx["snapshot"].get("trade_date", ""),
                                           "count": len(ups), "stocks": ups[:50],
                                           "source": "derived_from_market_list"}
            ctx["snapshot"]["limit_down"] = {"date": ctx["snapshot"].get("trade_date", ""),
                                             "count": len(downs), "stocks": downs[:50],
                                             "source": "derived_from_market_list"}

    # 3. 新闻与公告
    news = step("news", timed_source, "巨潮资讯-公告", news_col.collect_news, WATCH, 14)
    ctx["news"] = news or {"events": [], "issues": ["未采集"]}

    # 4. 数据质量检查
    cross_codes = [("sh600519", "600519", "1.600519"), ("sz000001", "000001", "0.000001"),
                   ("sh510300", "510300", "1.510300"), ("sh600036", "600036", "1.600036")]
    qual = step("quality", quality.run_quality_checks, ctx["snapshot"], ctx["market_list"],
                cross_codes)
    ctx["quality"] = qual or {"overall": "unknown"}

    # 5. 选股初筛
    cands_doc = step("screening", screener.run_screening, ctx["market_list"]["items"])
    ctx["candidates"] = cands_doc or {"candidates": [], "stats": {}}

    # 6. 可交易性分层筛选（第四阶段新增）
    ta = step("tradeability", tradeability.screen_tradable, ctx["market_list"]["items"])
    ctx["tradeability"] = ta or {"tradable": [], "blocked": [], "stats": {}}
    save_json(ctx["tradeability"], REPORTS + "/tradeability.json")

    # 7. 跨数据源核验（对候选）
    cv = step("cross_verify", crosscheck.verify_candidates,
              ctx["candidates"].get("candidates", []))
    ctx["cross_verify"] = cv or {"results": [], "summary": {}}
    save_json(ctx["cross_verify"], REPORTS + "/cross_verify.json")

    # 8. 财报数据（对候选）
    codes = [c["code"] for c in ctx["candidates"].get("candidates", [])]
    fund = step("fundamentals", fetch_fundamentals, codes)
    ctx["fundamentals"] = fund or {"data": {}, "meta": {"ok": False}}
    save_json(ctx["fundamentals"], REPORTS + "/fundamentals.json")

    # 9. 智谱独立研究
    research = step("research", build_research, ctx["snapshot"], ctx["market_list"],
                    ctx["candidates"]["candidates"], ctx["news"], ctx["quality"],
                    ctx["fundamentals"], ctx["cross_verify"], ctx["tradeability"])
    ctx["research"] = research or {}

    # 10. 反方审查（9 项）
    adv = step("adversarial", build_adversarial, ctx["candidates"]["candidates"],
               ctx["market_list"], ctx["snapshot"], ctx["news"], ctx["quality"],
               ctx["fundamentals"], ctx["cross_verify"], ctx["tradeability"])
    ctx["adversarial"] = adv or {"checks": [], "summary": {}}

    # 11. 账户状态
    pf = step("portfolio", L.portfolio)
    ctx["portfolio"] = pf or {}

    # 12. 预测复盘
    try:
        res = review_mod.resolve_due_predictions()
        summ = review_mod.review_summary()
        review_mod.write_review_notes(res, summ)
        save_json({"results": res, "summary": summ}, REPORTS + "/prediction_review.json")
    except Exception as e:
        log.error("预测复盘失败: %s", e)

    # 13. 记录本次预测（留痕）
    try:
        L.add_prediction(
            subject=f"市场环境@{ctx['snapshot'].get('trade_date')}",
            claim=json.dumps({
                "environment": [e.get("text") if isinstance(e, dict) else e
                                for e in ctx["research"].get("market_environment", [])],
                "top_candidates": [c["code"] for c in ctx["candidates"].get("candidates", [])][:5],
                "adversarial_summary": ctx["adversarial"].get("summary", {}),
            }, ensure_ascii=False),
            horizon="下一交易日")
    except Exception as e:
        log.warning("预测留痕失败: %s", e)

    # 14. v4 交接包（10 文件 + manifest + ZIP）
    ho = step("handoff", handoff_v4.export_handoff_v4, ctx["snapshot"], ctx["market_list"],
              ctx["news"], ctx["candidates"], ctx["research"], ctx["adversarial"],
              ctx["quality"], ctx["portfolio"])
    ctx["handoff"] = ho or {}

    # 15. 账本备份
    try:
        bk = L.backup()
        log.info("账本已备份: %s", bk)
    except Exception as e:
        log.error("账本备份失败: %s", e)

    # 16. 知识库同步
    synced = step("vault_sync", vault_sync.sync_daily,
                  ctx["snapshot"].get("trade_date") if ctx.get("snapshot") else None)

    audit("pipeline_end", run_id=run_id,
          quality=(ctx.get("quality") or {}).get("overall"),
          candidates=len((ctx.get("candidates") or {}).get("candidates", [])),
          handoff_id=(ctx.get("handoff") or {}).get("handoff_id"))

    print("\n===== 第四阶段流水线完成 =====")
    print("数据质量:", (ctx.get("quality") or {}).get("overall"))
    print("候选数量:", len((ctx.get("candidates") or {}).get("candidates", [])))
    ta_stats = (ctx.get("tradeability") or {}).get("stats", {})
    print("可交易性: 输入", ta_stats.get("input"), "→ 可交易", ta_stats.get("tradable"))
    cv_sum = (ctx.get("cross_verify") or {}).get("summary", {})
    print("跨源核验:", cv_sum)
    fm = (ctx.get("fundamentals") or {}).get("meta", {})
    print("财报匹配:", fm.get("matched"), "只（报告期", fm.get("report_date"), "）")
    print("反方审查:", (ctx.get("adversarial") or {}).get("summary", {}).get("conclusion"))
    print("账本对账:", "通过" if ctx["ledger"].get("reconcile", {}).get("ok") else "失败")
    print("交接包:", (ctx.get("handoff") or {}).get("handoff_id"))
    print("ZIP:", (ctx.get("handoff") or {}).get("zip"))
    print("数据源健康:", {k: v.get("status") for k, v in HEALTH.state.items()})
    return ctx


if __name__ == "__main__":
    skip = "--skip-scan" in sys.argv
    main(skip_scan=skip)
