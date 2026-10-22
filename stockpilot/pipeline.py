"""StockPilot 第三阶段端到端流水线。

在第二阶段链路基础上增加：
- 时间管理（Asia/Shanghai、八类时间戳、陈旧度标记）
- 数据源健康记录与降级标记
- 账本迁移自愈（init_db 幂等）
- 预测复盘（到期结算）
- GPT-6 研究交接包（MD+JSON+唯一ID+校验和）
- 知识库全量同步（含事件/公司研究/检索）
- 账本在线备份

用法：
    python -m stockpilot.pipeline                # 完整流程
    python -m stockpilot.pipeline --skip-scan    # 复用全市场列表
    python -m stockpilot.pipeline --handoff-only # 只用现有数据生成交接包
"""
import datetime as dt
import json
import os
import sys
import time

from stockpilot import REPORTS, VAULT, audit, iso, load_json, save_json, setup_logger
from stockpilot import ledger as L
from stockpilot import report as report_mod
from stockpilot import review as review_mod
from stockpilot import source_health as sh
from stockpilot import timeutil as tu
from stockpilot import vault_sync
from stockpilot import handoff as handoff_mod
from stockpilot.collectors import news as news_col
from stockpilot.collectors import quote as q
from stockpilot import quality, screener
from stockpilot.research import build_adversarial, build_research

log = setup_logger("pipeline", "pipeline.log")

WATCH = [("600519", "gssh0600519"), ("000001", "gssz0000001"), ("600036", "gssh0600036")]
HEALTH = sh.SourceHealth()


def step(name, fn, *args, **kwargs):
    """执行一个步骤，记录耗时与结果，失败不中断整体流程"""
    t0 = time.time()
    log.info("STEP %s 开始", name)
    try:
        res = fn(*args, **kwargs)
        log.info("STEP %s 完成 耗时 %.1fs", name, time.time() - t0)
        audit("pipeline_step", step=name, ok=True, elapsed=round(time.time() - t0, 1))
        return res
    except Exception as e:
        import traceback
        log.error("STEP %s 失败: %s", name, e)
        audit("pipeline_step", step=name, ok=False, error=f"{type(e).__name__}: {e}",
              trace=traceback.format_exc()[-600:])
        return None


def collect_market_list_tracked():
    """带健康记录的全市场列表采集"""
    if not HEALTH.is_available("sina.market_list"):
        raise IOError("数据源 sina.market_list 处于熔断状态，跳过采集")
    res, err = sh.with_retry(q.collect_market_list, retries=2, backoff=1.5,
                             label="market_list")
    if err:
        HEALTH.record_fail("sina.market_list", err)
        raise IOError(err)
    HEALTH.record_ok("sina.market_list", note=f"{len(res.get('items', []))} 只")
    return res


def main(skip_scan=False, handoff_only=False):
    run_id = tu.now().strftime("%Y%m%d_%H%M%S")
    log.info("===== StockPilot 第三阶段流水线启动 run_id=%s =====", run_id)
    audit("pipeline_start", run_id=run_id, stage="v3", skip_scan=skip_scan,
          handoff_only=handoff_only)
    ctx = {}

    # 0. 账本自愈（幂等初始化 + 对账）
    def ledger_setup():
        L.init_db()
        rec = L.reconcile()
        integ = L.integrity_check()
        if not rec["ok"]:
            audit("ledger_reconcile_failed", detail=rec)
            log.error("账本对账失败: %s", rec)
        if not integ:
            audit("ledger_integrity_failed")
            log.error("数据库完整性检查失败")
        return {"reconcile": rec, "integrity": integ}
    ctx["ledger"] = step("ledger_check", ledger_setup) or {}

    if handoff_only:
        return build_outputs(ctx, run_id, handoff_only=True)

    # 1. 行情快照
    snap = step("market_snapshot", q.collect_market_snapshot)
    ctx["snapshot"] = snap or {}

    # 2. 全市场列表（带健康记录）
    if skip_scan and os.path.exists(REPORTS + "/market_list.json"):
        ml = load_json(REPORTS + "/market_list.json")
        log.info("复用已有全市场列表（--skip-scan）")
    else:
        ml = step("market_list", collect_market_list_tracked)
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
            log.info("涨停池接口不可用，已用全市场列表推算：涨 %d 跌 %d", len(ups), len(downs))

    # 3. 新闻与公告
    news = step("news", news_col.collect_news, WATCH, 14)
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

    # 6. 智谱独立研究
    research = step("research", build_research, ctx["snapshot"], ctx["market_list"],
                    ctx["candidates"]["candidates"], ctx["news"], ctx["quality"])
    ctx["research"] = research or {}

    # 7. 反方审查
    adv = step("adversarial", build_adversarial, ctx["candidates"]["candidates"],
               ctx["market_list"], ctx["snapshot"], ctx["news"], ctx["quality"])
    ctx["adversarial"] = adv or {"checks": [], "summary": {}}

    # 8. 账户状态
    pf = step("portfolio", L.portfolio)
    ctx["portfolio"] = pf or {}

    # 9. 导出报告
    paths = step("export", report_mod.export_all, ctx["snapshot"], ctx["market_list"],
                 ctx["news"], ctx["candidates"], ctx["research"], ctx["adversarial"],
                 ctx["portfolio"], ctx["quality"])
    ctx["paths"] = paths or {}
    save_json(ctx["quality"], REPORTS + "/quality_report.json")

    return build_outputs(ctx, run_id)


def build_outputs(ctx, run_id, handoff_only=False):
    """预测复盘、交接包、知识库、备份（handoff_only 时只用磁盘现有报告）"""
    # 10. 预测复盘（到期结算）
    try:
        res = review_mod.resolve_due_predictions()
        summ = review_mod.review_summary()
        review_mod.write_review_notes(res, summ)
        save_json({"results": res, "summary": summ}, REPORTS + "/prediction_review.json")
        log.info("预测复盘完成：已结算 %s，未到期 %s",
                 sum(1 for r in res if r["status"] == "resolved"),
                 sum(1 for r in res if r["status"] == "pending"))
    except Exception as e:
        log.error("预测复盘失败: %s", e)
        audit("review_failed", error=str(e))

    # 11. 记录本次预测（留痕，供未来复盘）
    try:
        snap = ctx.get("snapshot") or load_json(REPORTS + "/market_snapshot.json")
        cands = ctx.get("candidates") or load_json(REPORTS + "/stock_candidates.json")
        adv = ctx.get("adversarial") or load_json(REPORTS + "/risk_review.json")
        research = ctx.get("research") or load_json(REPORTS + "/research.json")
        L.add_prediction(
            subject=f"市场环境@{snap.get('trade_date')}",
            claim=json.dumps({
                "environment": research.get("market_environment", []),
                "top_candidates": [c["code"] for c in cands.get("candidates", [])][:5],
                "adversarial_summary": adv.get("summary", {}),
            }, ensure_ascii=False),
            horizon="下一交易日")
    except Exception as e:
        log.warning("预测留痕失败: %s", e)

    # 12. GPT-6 研究交接包
    try:
        snap = ctx.get("snapshot") or load_json(REPORTS + "/market_snapshot.json")
        ml = ctx.get("market_list") or load_json(REPORTS + "/market_list.json")
        news = ctx.get("news") or load_json(REPORTS + "/news_events.json")
        cands = ctx.get("candidates") or load_json(REPORTS + "/stock_candidates.json")
        research = ctx.get("research") or load_json(REPORTS + "/research.json")
        adv = ctx.get("adversarial") or load_json(REPORTS + "/risk_review.json")
        qual = ctx.get("quality") or load_json(REPORTS + "/quality_report.json")
        pf = ctx.get("portfolio") or L.portfolio()
        pkg = handoff_mod.build_handoff(snap, ml, news, cands, research, adv, qual, pf,
                                        prev_handoff_path=handoff_mod.latest_handoff_path())
        handoff_mod.export_handoff(pkg)
        log.info("交接包 %s 生成，完整性 %s", pkg["handoff_id"], pkg["integrity"]["complete"])
    except Exception as e:
        log.error("交接包生成失败: %s", e)
        audit("handoff_failed", error=str(e))

    # 13. 账本备份（官方在线备份）
    try:
        bk = L.backup()
        log.info("账本已备份: %s", bk)
    except Exception as e:
        log.error("账本备份失败: %s", e)

    # 14. 知识库同步
    trade_date = ctx.get("snapshot", {}).get("trade_date") if ctx.get("snapshot") else None
    synced = step("vault_sync", vault_sync.sync_daily, trade_date)
    if synced:
        log.info("知识库同步: %s", list(synced.keys()))

    audit("pipeline_end", run_id=run_id,
          quality=(ctx.get("quality") or {}).get("overall"),
          candidates=len((ctx.get("candidates") or {}).get("candidates", [])))

    print("\n===== 流水线完成 =====")
    if ctx.get("quality"):
        print("数据质量:", ctx["quality"].get("overall"))
    if ctx.get("candidates"):
        print("候选数量:", len(ctx["candidates"].get("candidates", [])))
    if ctx.get("ledger"):
        print("账本对账:", "通过" if ctx["ledger"].get("reconcile", {}).get("ok") else "失败",
              "| 完整性:", "通过" if ctx["ledger"].get("integrity") else "失败")
    print("数据源健康:", {k: v.get("status") for k, v in HEALTH.state.items()})
    print("交接包: reports/handoff_latest.md")
    return ctx


if __name__ == "__main__":
    skip = "--skip-scan" in sys.argv
    ho = "--handoff-only" in sys.argv
    main(skip_scan=skip, handoff_only=ho)
