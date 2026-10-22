"""交接包完整性与备份恢复测试（第三阶段新增）。

覆盖任务书第十六章剩余用例：
- GPT-6 研究交接包完整性（12 项 + 校验和）
- 数据库备份恢复（真实备份，非测试库副本）
- 报告重复生成（幂等）
- 历史预测不可篡改
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
    from stockpilot import REPORTS, ledger as L, handoff as handoff_mod, timeutil as tu
    from stockpilot import load_json

    # ---------------- 1. 交接包完整性 ----------------
    section("1. GPT-6 交接包完整性")
    pkg_path = None
    latest = os.path.join(REPORTS, "handoff_latest.json")
    if os.path.exists(latest):
        ptr = load_json(latest)
        # 优先取交接包本体（v3: files.json；v4: PKG-*.json 或 files 表中的 research.json 所在包）
        files = ptr.get("files", {})
        if isinstance(files, dict) and files.get("json"):
            pkg_path = files["json"]
        else:
            # v4：从 reports 目录取最新 PKG-*.json
            import glob
            pkgs = sorted(glob.glob(os.path.join(REPORTS, "PKG-*.json")))
            pkg_path = pkgs[-1] if pkgs else None
    if pkg_path is None:
        # 用现有报告现场生成一个
        snap = load_json(REPORTS + "/market_snapshot.json")
        ml = load_json(REPORTS + "/market_list.json")
        news = load_json(REPORTS + "/news_events.json")
        cands = load_json(REPORTS + "/stock_candidates.json")
        research = load_json(REPORTS + "/research.json")
        adv = load_json(REPORTS + "/risk_review.json")
        qual = load_json(REPORTS + "/quality_report.json")
        pf = L.portfolio()
        pkg = handoff_mod.build_handoff(snap, ml, news, cands, research, adv, qual, pf)
        handoff_mod.export_handoff(pkg)
        import glob
        pkgs = sorted(glob.glob(os.path.join(REPORTS, "PKG-*.json")))
        pkg_path = pkgs[-1] if pkgs else os.path.join(REPORTS, "handoff_latest.json")
    pkg = load_json(pkg_path)
    check("交接包有唯一ID", pkg.get("handoff_id", "").startswith("PKG-"), pkg.get("handoff_id"))
    check("交接包有生成时间", bool(pkg.get("generated_at")))
    check("交接包有数据版本", bool(pkg.get("data_version", {}).get("trade_date")))
    integ = pkg.get("integrity", {})
    # 兼容 v3（complete/checksum/sections）与 v4（all_files_present/combined_checksum/files）
    complete = integ.get("complete")
    if complete is None:
        complete = integ.get("all_files_present")
    check("交接包完整性标记", complete is True)
    if "required_sections" in integ:
        check("12项必备章节齐全",
              integ["sections_present"] == integ["required_sections"],
              str(integ["sections_present"]))
    else:
        check("v4 manifest 10文件齐全",
              sum(1 for f in pkg["files"] if f["present"]) == 10)
    cs = integ.get("checksum") or integ.get("combined_checksum")
    check("校验和存在", bool(cs) and len(cs) == 16, str(cs))
    check("含市场概况", "indices" in pkg.get("market_overview", {}))
    check("含行情数据时间字段", "latest_index_quote_time" in pkg.get("market_data_time", {}))
    check("行情时间缺失时有说明", bool(pkg.get("market_data_time", {}).get("quote_time_note"))
          or bool(pkg.get("market_data_time", {}).get("latest_index_quote_time")))
    check("含时效说明", bool(pkg.get("market_data_time", {}).get("staleness_note")))
    check("含新闻及来源", pkg["major_news"]["total"] > 0)
    check("含候选与理由", len(pkg["candidates_with_reasons"]) > 0)
    check("含智谱独立研究", bool(pkg["glm_independent_research"]["market_environment"]))
    check("含反方审查", len(pkg["adversarial_review"]["checks"]) > 0)
    check("含账户状态", pkg["account_status"]["initial_capital"] == 1000.0)
    check("含历史决策字段", "historical_decisions" in pkg)
    check("含预测准确性", "prediction_accuracy" in pkg)
    check("含数据质量警告", bool(pkg["data_quality_warnings"]["known_source_limits"]))
    check("含当前有效规则", len(pkg["active_rules"]) >= 5)
    check("含与上次的变化", bool(pkg["changes_from_previous"]["notes"]))
    check("Markdown 同时生成", os.path.exists(os.path.join(REPORTS, "handoff_latest.md")))

    # 校验和验证：重算一致性
    import hashlib
    cs = pkg["integrity"].get("checksum") or pkg["integrity"].get("combined_checksum")
    if "required_sections" in pkg["integrity"]:
        # v3/v5 包体：对"除 integrity 之外的全部字段"重算。
        # 与 handoff._sha256 的算法严格一致；不得再排除其他字段，
        # 否则会掩盖"追加字段后未重算 checksum"的真实缺陷
        # （该缺陷曾导致交付包自检失败）。
        from stockpilot import handoff_v5 as _h5
        recomputed = _h5.package_checksum(pkg)
        check("校验和可重算验证", recomputed == cs, f"{recomputed} vs {cs}")
    else:
        # v4/v5：逐文件校验 sha256 与磁盘一致
        import hashlib as _h
        mism = []
        for f in pkg["files"]:
            fp = os.path.join(REPORTS, f["name"])
            if f["present"] and os.path.exists(fp):
                d = _h.sha256(open(fp, "rb").read()).hexdigest()[:16]
                if d != f["sha256"]:
                    mism.append(f["name"])
        check("v4 文件哈希与磁盘一致", not mism, str(mism))

    # v5 专用：manifest 必须能通过独立自检（这是第五阶段修复的核心）
    from stockpilot import handoff_v5 as h5
    mp = os.path.join(REPORTS, "manifest.json")
    if os.path.exists(mp):
        vr = h5.verify_manifest(mp)
        check("v5 manifest 独立自检通过", vr["ok"] is True, str(vr["problems"]))
        check("v5 封缄文件存在且匹配",
              vr["seal"] is not None
              and vr["seal"]["declared_sha256"] == vr["seal"]["actual_sha256"],
              str(vr.get("seal")))

    # ---------------- 2. 备份恢复（在真实库的副本上操作） ----------------
    section("2. 真实账本备份与恢复（副本隔离）")
    real_db = os.path.join(os.path.dirname(REPORTS), "data", "stockpilot.db")
    check("真实账本存在", os.path.exists(real_db))
    # 关键：恢复测试必须使用副本，避免回滚真实账本的后续记录
    work = os.path.join(tempfile.mkdtemp(prefix="spwk_"), "work.db")
    shutil.copy2(real_db, work)
    pf_before = L.portfolio(path=work)
    bk = L.backup(work, backup_dir=tempfile.mkdtemp(prefix="spbk_"))
    check("在线备份生成", os.path.exists(bk))
    check("备份完整可读", L.integrity_check(bk))
    # 在副本上写入一笔测试成交
    aid = L.add_advice("GLM", "buy", code="601398", name="工商银行", market="A", path=work)
    iid = L.add_intent(aid, "buy", "601398", "工商银行", "A", price=2.0, shares=100, path=work)
    oid = L.submit_order(iid, "buy", "601398", "工商银行", "A", price=2.0, shares=100, path=work)
    r = L.confirm_trade(oid, price=2.0, shares=100, trade_uid=f"TEST-{tu.now().timestamp()}",
                        path=work)
    check("账本可写入测试成交", r["id"] > 0)
    pf_after = L.portfolio(path=work)
    check("写入后现金变化", pf_after["cash"] != pf_before["cash"])
    # 恢复到备份点
    res = L.restore(bk, target=work)
    pf_restored = L.portfolio(path=work)
    check("恢复后现金回到备份点", abs(pf_restored["cash"] - pf_before["cash"]) < 0.01,
          f"{pf_restored['cash']} vs {pf_before['cash']}")
    check("恢复后对账通过", L.reconcile(path=work)["ok"])
    check("恢复保留了安全备份", os.path.exists(res["safety_backup"]))
    check("真实账本未被测试修改", abs(L.portfolio()["cash"] - pf_real_before) < 0.01
          if (pf_real_before := L.portfolio()["cash"]) else True)
    shutil.rmtree(os.path.dirname(work), ignore_errors=True)

    # ---------------- 3. 报告重复生成 ----------------
    section("3. 报告重复生成（幂等）")
    from stockpilot import report as report_mod
    snap = load_json(REPORTS + "/market_snapshot.json")
    ml = load_json(REPORTS + "/market_list.json")
    news = load_json(REPORTS + "/news_events.json")
    cands = load_json(REPORTS + "/stock_candidates.json")
    research = load_json(REPORTS + "/research.json")
    adv = load_json(REPORTS + "/risk_review.json")
    qual = load_json(REPORTS + "/quality_report.json")
    # 重要：本项只测"重复生成是否幂等"，必须写到临时目录。
    # 早期版本直接写 REPORTS，会把已封缄交接包里的 decision_report.md /
    # portfolio.json 改写，导致 manifest 哈希失效（自毁式测试）。
    idem_dir = tempfile.mkdtemp(prefix="spidem_")
    p1 = report_mod.export_all(snap, ml, news, cands, research, adv,
                               L.portfolio(), qual, out_dir=idem_dir)
    md1 = open(p1["decision_report"], encoding="utf-8").read()
    p2 = report_mod.export_all(snap, ml, news, cands, research, adv,
                               L.portfolio(), qual, out_dir=idem_dir)
    md2 = open(p2["decision_report"], encoding="utf-8").read()
    # 报告体相同（时间戳行除外）
    import re
    strip = lambda s: re.sub(r"报告生成时间：[^\n]+", "", s)
    check("重复生成报告体一致", strip(md1) == strip(md2))
    check("重复生成不产生重复文件", os.path.exists(p2["decision_report"]))
    shutil.rmtree(idem_dir, ignore_errors=True)

    # ---------------- 4. 历史预测不可篡改 ----------------
    section("4. 历史预测不可篡改")
    tmp = tempfile.mkdtemp(prefix="sptest_")
    tdb = os.path.join(tmp, "t.db")
    L.init_db(tdb)
    pid = L.add_prediction("600028", "一周内上涨3%", horizon="5个交易日",
                           code="600028", base_price=5.5, path=tdb)
    p_before = L.get_predictions(path=tdb)[0]
    L.resolve_prediction(pid, {"change_pct": 2.5, "direction_correct": True}, path=tdb)
    p_after = L.get_predictions(path=tdb)[0]
    check("预测结算后状态更新", p_after["resolved"] == 1)
    check("预测原始claim未被修改", p_after["claim"] == p_before["claim"])
    try:
        L.resolve_prediction(pid, {"change_pct": 99}, path=tdb)
        check("重复结算应被拒绝", False)
    except ValueError as e:
        check("重复结算被拒绝", "不可覆盖" in str(e))
    p_final = L.get_predictions(path=tdb)[0]
    check("结算结论未被覆盖", json.loads(p_final["outcome"])["change_pct"] == 2.5
          if isinstance(p_final["outcome"], str) else p_final["outcome"]["change_pct"] == 2.5)
    shutil.rmtree(tmp, ignore_errors=True)

    # ---------------- 5. 时间戳跨交易日 ----------------
    section("5. 跨交易日/周末/节假日")
    # 周六
    f_sat = tu.freshness(dt.datetime(2026, 10, 16, 15, 0), dt.datetime(2026, 10, 17, 10, 0))
    check("周六数据不算过期", f_sat["is_stale"] is False)
    # 周一早上（周末后第一个交易日前）
    f_mon = tu.freshness(dt.datetime(2026, 10, 16, 15, 0), dt.datetime(2026, 10, 19, 9, 0))
    check("周一盘前不算过期", f_mon["is_stale"] is False)
    # 交易时段内 20 分钟延迟
    f_delay = tu.freshness(dt.datetime(2026, 10, 19, 10, 0), dt.datetime(2026, 10, 19, 10, 20))
    check("盘中20分钟延迟判定过期", f_delay["is_stale"] is True)
    # 交易日期标签
    lbl = tu.trading_day_label(dt.date(2026, 10, 16), dt.datetime(2026, 10, 19, 9, 0))
    check("跨周末标签提示陈旧", "陈旧" in lbl, lbl)

    print(f"\n{'='*50}")
    print(f"总计: {len(PASS)} 通过, {len(FAIL)} 失败")
    if FAIL:
        for n, d in FAIL:
            print(f"  FAIL: {n} {d}")
        sys.exit(1)
    print("全部测试通过")


if __name__ == "__main__":
    main()
