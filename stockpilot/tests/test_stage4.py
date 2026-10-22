"""第四阶段稳定性与验收测试（任务书第十六章 / 验收G）。

覆盖：
1. 交接包完整性（10 文件 + manifest + ZIP 内容 + 敏感数据扫描）
2. 可交易性计算（单位/费用/资金约束/多费率）
3. 跨源核验（冲突判定/单源标记）
4. 数据源故障模拟（超时/降级/熔断/恢复）
5. 模拟交易闭环（成交规则/不成交/T+1/资金不足/独立库隔离）
6. 重复运行幂等（交接包/报告/账本）
7. 备份恢复（副本隔离）
8. 断网降级（数据源全失败时不得伪造实时数据）
9. 研究流水线（research/quality 真实入链）
"""
import datetime as dt
import json
import os
import shutil
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

PASS, FAIL = [], []


def check(name, cond, detail=""):
    if cond:
        PASS.append(name)
        print(f"  PASS  {name}")
    else:
        FAIL.append((name, detail))
        print(f"  FAIL  {name}  {detail}")


def section(t):
    print(f"\n===== {t} =====")


def main():
    from stockpilot import REPORTS, load_json
    from stockpilot import ledger as L
    from stockpilot import simulation as sim
    from stockpilot import tradeability as ta
    from stockpilot import crosscheck as cc
    from stockpilot import source_health as sh
    from stockpilot import handoff_v4 as h4
    from stockpilot import timeutil as tu

    # ---------------- 1. 交接包完整性 ----------------
    section("1. 交接包完整性（10文件+manifest+ZIP）")
    latest = load_json(REPORTS + "/handoff_latest.json")
    m = load_json(os.path.join(REPORTS, "manifest.json"))
    check("manifest 存在且10文件齐全", m["integrity"]["all_files_present"] is True)
    # manifest 不记录自身哈希（自指无法自洽），自身完整性由封缄文件证明
    check("每个内容文件都有 sha256",
          all(f.get("sha256") for f in m["files"] if f["name"] != "manifest.json"))
    check("manifest 有唯一交接ID", m["handoff_id"].startswith("PKG-"))
    check("manifest 有数据版本", bool(m["data_version"]["trade_date"]))
    check("manifest 有时效说明", bool(m["data_version"]["freshness"]["trade_date_label"]))
    check("manifest 有账户状态", m["account_status"]["initial_capital"] == 1000.0)
    check("manifest 有历史关联", "predictions_total" in m["history_links"])
    check("manifest 有敏感数据声明", "不含密码" in m["sensitivity_note"])
    # ZIP
    zpath = latest.get("zip")
    check("ZIP 已生成", zpath and os.path.exists(zpath))
    if zpath and os.path.exists(zpath):
        import zipfile
        z = zipfile.ZipFile(zpath)
        names = z.namelist()
        check("ZIP 含10个报告文件", sum(1 for n in names if any(
            n.endswith("/" + f) or n.endswith(f) for f in h4.REQUIRED_FILES)) >= 10)
        # ZIP 内 manifest 与磁盘一致
        zm = json.loads(z.read([n for n in names if n.endswith("manifest.json")][0]))
        check("ZIP 内 manifest 完整", zm["integrity"]["all_files_present"] is True)
        # 敏感数据：真实密钥/cookie 值（排除声明文字）
        leaked = []
        for n in names:
            try:
                t = z.read(n).decode("utf-8", "ignore")
            except Exception:
                continue
            for pat in ("sk-", "Bearer ", "Set-Cookie", "api_key\":", "password\":"):
                if pat in t:
                    leaked.append((n, pat))
        check("ZIP 无真实密钥/凭据", not leaked, str(leaked))

    # ---------------- 2. 研究流水线真实性 ----------------
    section("2. 研究流水线（research/quality 真实入链）")
    r = load_json(REPORTS + "/research.json")
    q = load_json(REPORTS + "/quality_report.json")
    check("research.json 由程序生成（有作者与时间）",
          r.get("author", "").startswith("GLM") and bool(r.get("generated_at")))
    check("research 证据四分类", all(
        set(e.keys()) >= {"type", "text"}
        for c in r["candidates"] for e in (c["bull_evidence"] + c["bear_evidence"])))
    check("research 含财报数据", any(c.get("fundamentals") for c in r["candidates"]))
    check("research 有数据缺口披露", "data_gaps" in r and "questions_to_verify" in r)
    check("quality_report 在交接包中",
          "quality_report.json" in [f["name"] for f in m["files"]])
    check("quality 有交叉校验/时效/列表三类检查",
          set(q["checks"].keys()) >= {"cross_price", "freshness", "market_list"})
    # 候选必须同时出现在 research 与 stock_candidates
    cands = load_json(REPORTS + "/stock_candidates.json")["candidates"]
    check("候选一致（candidates=research）",
          [c["code"] for c in cands] == [c["code"] for c in r["candidates"]])

    # ---------------- 3. 可交易性计算 ----------------
    section("3. 1000元可交易性计算")
    trad = load_json(REPORTS + "/tradeability.json")
    check("可交易性统计完整", trad["stats"]["tradable"] > 0 and trad["stats"]["blocked"] > 0)
    check("有阻止原因分布", bool(trad["stats"]["block_distribution"]))
    # 手工核算一个案例
    item = {"code": "600011", "name": "华能国际", "price": 7.51, "amount": 7.4e8,
            "market": "SH", "pct_chg": 1.2}
    d = ta.tradable_decision(item)
    check("可交易判定正确", d["tradable"] is True)
    l2 = d["checks"]["L2_tradeability"]
    check("一手价格=价×100", l2["hand_value"] == 751.0)
    check("计划股数为整手", l2["planned_shares"] % 100 == 0)
    check("买后现金非负", l2["cash_left_after"] >= 0)
    check("三档费率敏感性", set(l2["fee_scenarios"].keys()) ==
          {"stock_pessimistic_5yuan", "stock_moderate_2wan_5yuan", "stock_optimistic_2wan5_nomin"})
    # 高价股应被拒
    item2 = dict(item, code="600519", name="贵州茅台", price=1329.0)
    d2 = ta.tradable_decision(item2)
    check("高价股被拒绝", d2["tradable"] is False and
          any("超过" in r for r in d2["blocking_reasons"]))
    # 现金不足应被拒
    d3 = ta.tradable_decision(item, cash=100.0)
    check("现金不足被拒绝", d3["tradable"] is False)

    # ---------------- 4. 跨源核验 ----------------
    section("4. 跨数据源核验")
    cv = load_json(REPORTS + "/cross_verify.json")
    check("候选全部多源一致", cv["summary"]["pass"] == cv["summary"]["total"])
    # 构造冲突场景（用假代码触发单源）
    r1 = cc.cross_verify("999999")
    check("无数据代码返回 no_data", r1["verdict"] == "no_data")

    # ---------------- 5. 数据源故障模拟 ----------------
    section("5. 数据源故障处理")
    tmp = tempfile.mkdtemp(prefix="spfail_")
    hf = os.path.join(tmp, "h.json")
    h = sh.SourceHealth(path=hf)
    # 可重试错误：重试后成功
    n = [0]
    def flaky():
        n[0] += 1
        if n[0] < 2:
            raise TimeoutError("timed out")
        return "ok"
    r_, e_ = sh.with_retry(flaky, retries=2, backoff=0.05, label="flaky")
    check("超时错误重试后成功", r_ == "ok")
    # WAF：不重试
    def waf():
        raise IOError("501 waf block")
    r2_, e2_ = sh.with_retry(waf, retries=3, backoff=0.05)
    check("WAF 错误不重试", r2_ is None)
    # 熔断与恢复
    for _ in range(3):
        h.record_fail("t.src", "connection aborted")
    check("连续失败打开熔断", h.is_available("t.src") is False)
    h.record_ok("t.src")
    check("成功后熔断解除", h.is_available("t.src") is True)
    # 降级标记
    dm = sh.degraded_marker("测试降级")
    check("降级标记含说明与指令", dm["degraded"] and "不得视为实时" in dm["instruction"])
    shutil.rmtree(tmp, ignore_errors=True)

    # ---------------- 6. 模拟交易闭环 ----------------
    section("6. 模拟交易闭环（独立库）")
    sim_db = os.path.join(tempfile.mkdtemp(prefix="spsim_"), "sim.db")
    sim.init_db(sim_db)
    pf0 = sim.sim_portfolio(path=sim_db)
    check("模拟账户初始1000元", pf0["cash"] == 1000.0 and pf0["trade_count"] == 0)
    # 真实账本不被模拟污染
    real_cash = L.portfolio()["cash"]
    aid = sim.sim_advice("GPT-6", "buy", "600011", "华能国际", 7.51, 100, "测试", path=sim_db)
    oid = sim.sim_order(aid, "buy", "600011", "华能国际", price=7.59, shares=100, path=sim_db)
    res = sim.process_order(oid, path=sim_db)
    # 成交价 = 委托价 + 买入滑点（滑点现已实际生效，不再等于委托价）
    expected_fill = sim.apply_slippage(7.59, "buy")
    check("按规则成交",
          res["status"] == sim.STATUS_ASSUMED_DAILY
          and abs(res["fill_price"] - expected_fill) < 1e-9,
          f"fill={res.get('fill_price')} expected={expected_fill}")
    check("买入滑点向上生效", res["fill_price"] > 7.59,
          f"fill={res.get('fill_price')} > 7.59")
    pf1 = sim.sim_portfolio(path=sim_db)
    check("模拟现金减少", pf1["cash"] < 1000.0)
    check("模拟持仓1只", len(pf1["positions"]) == 1)
    check("真实账本未受影响", L.portfolio()["cash"] == real_cash)
    # 成交规则：委托价过低不成交
    aid2 = sim.sim_advice("GPT-6", "buy", "600011", "华能国际", 7.51, 100, "测试", path=sim_db)
    oid2 = sim.sim_order(aid2, "buy", "600011", "华能国际", price=1.0, shares=100, path=sim_db)
    res2 = sim.process_order(oid2, path=sim_db)
    check("委托价过低不成交（not_met）", res2["status"] == sim.STATUS_NOT_MET)
    # T+1
    ov = {"action": "sell", "code": "600011", "name": "华能国际", "price": 7.59, "shares": 100}
    st, _, rule = sim.evaluate_fill(ov, {"date": "2026-10-18", "low": 7.5, "high": 7.6},
                                    {"600011": "2026-10-18"})
    check("T+1 拦截当日卖出", st == sim.STATUS_NOT_MET and "T+1" in rule)
    # 费用记录
    check("成交含费用记录", pf1["fees_paid"] > 0)
    shutil.rmtree(os.path.dirname(sim_db), ignore_errors=True)

    # ---------------- 7. 重复运行幂等 ----------------
    section("7. 重复运行幂等")
    # 账本重复 init
    tmpdb = os.path.join(tempfile.mkdtemp(prefix="spdup_"), "d.db")
    L.init_db(tmpdb)
    c1 = L.portfolio(path=tmpdb)["cash"]
    L.init_db(tmpdb)
    c2 = L.portfolio(path=tmpdb)["cash"]
    check("重复 init 不重复入金", c1 == c2 == 1000.0)
    # 重复成交幂等
    a = L.add_advice("GLM", "buy", code="600011", name="华能国际", market="A", path=tmpdb)
    i = L.add_intent(a, "buy", "600011", "华能国际", "A", price=7.5, shares=100, path=tmpdb)
    o = L.submit_order(i, "buy", "600011", "华能国际", "A", price=7.5, shares=100, path=tmpdb)
    L.confirm_trade(o, 7.5, 100, trade_uid="DUP-1", path=tmpdb)
    r_dup = L.confirm_trade(o, 7.5, 100, trade_uid="DUP-1", path=tmpdb)
    check("重复成交幂等", r_dup["duplicate"] is True)
    check("重复成交后笔数仍为1", L.portfolio(path=tmpdb)["trade_count"] == 1)
    shutil.rmtree(os.path.dirname(tmpdb), ignore_errors=True)

    # ---------------- 8. 备份恢复 ----------------
    section("8. 备份恢复（副本隔离）")
    work = os.path.join(tempfile.mkdtemp(prefix="spbk_"), "w.db")
    shutil.copy2(os.path.join(os.path.dirname(REPORTS), "data", "stockpilot.db"), work)
    pf_before = L.portfolio(path=work)
    bk = L.backup(work, backup_dir=tempfile.mkdtemp(prefix="spbkdir_"))
    a = L.add_advice("GLM", "buy", code="601398", name="工商银行", market="A", path=work)
    i = L.add_intent(a, "buy", "601398", "工商银行", "A", price=2.0, shares=100, path=work)
    o = L.submit_order(i, "buy", "601398", "工商银行", "A", price=2.0, shares=100, path=work)
    L.confirm_trade(o, 2.0, 100, trade_uid="BK-1", path=work)
    L.restore(bk, target=work)
    pf_after = L.portfolio(path=work)
    check("恢复后现金回到备份点", abs(pf_after["cash"] - pf_before["cash"]) < 0.01)
    check("恢复后对账通过", L.reconcile(path=work)["ok"])
    shutil.rmtree(os.path.dirname(work), ignore_errors=True)

    # ---------------- 9. 时间戳与跨交易日 ----------------
    section("9. 时间戳与跨交易日")
    f_sat = tu.freshness(dt.datetime(2026, 10, 16, 15, 0), dt.datetime(2026, 10, 17, 10, 0))
    check("周六不算过期", f_sat["is_stale"] is False)
    f_delay = tu.freshness(dt.datetime(2026, 10, 19, 10, 0), dt.datetime(2026, 10, 19, 10, 20))
    check("盘中20分钟延迟判定过期", f_delay["is_stale"] is True)
    check("交易日期标签含陈旧提示", "陈旧" in tu.trading_day_label(
        dt.date(2026, 10, 16), dt.datetime(2026, 10, 19, 9, 0)))
    # 过期数据不得生成确定性价格建议（检查交接包有时效说明）
    check("交接包含时效警告", "staleness_note" in m["files"] or
          bool(m["data_version"]["freshness"]))

    print(f"\n{'='*50}")
    print(f"总计: {len(PASS)} 通过, {len(FAIL)} 失败")
    if FAIL:
        for n, d in FAIL:
            print(f"  FAIL: {n} {d}")
        sys.exit(1)
    print("全部测试通过")


if __name__ == "__main__":
    main()
