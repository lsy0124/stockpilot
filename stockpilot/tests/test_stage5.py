"""第五阶段回归测试（新增）。

这批测试专门锁定第五阶段复核中发现并修复的 9 类缺陷，
确保同一错误不会再次交付。每项测试都对应一个真实发生过的失败模式：

1. manifest 完整性自检（原实现只断言自我声明的布尔值，从不重算哈希）
2. manifest 自身哈希不得沿用上一阶段旧值（原 v5 残留 v4 的 3236 字节自哈希块）
3. 声明存在的文件必须真的在磁盘上（原 fee_model/data_quality/market_env 仅存在于 ZIP 内）
4. combined_checksum 必须能用真实字节重算
5. history_links 必须来自账本，不得硬编码 0
6. handoff_latest.json 的 package 指针必须指向真实存在的包本体
7. 费率唯一来源：模拟成交费用必须与 fee_model 完全一致（原为双份实现）
8. 审计字段映射：新闻真实字段 sec_code；候选财务/费用来自 research.json
9. 审计严重度：warn 级问题必须翻转 verdict，指数已知限制不得判 fail
"""
import datetime as dt
import hashlib
import json
import os
import shutil
import sys
import tempfile
import zipfile

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
    from stockpilot import REPORTS, load_json, save_json
    from stockpilot import ledger as L
    from stockpilot import fee_model
    from stockpilot import simulation as sim
    from stockpilot import research_audit as ra
    from stockpilot import handoff_v5 as h5

    # ---------------- 1. manifest 自检必须能抓出真实缺陷 ----------------
    section("1. manifest 完整性自检（回归：原实现只信自我声明）")
    tmp = tempfile.mkdtemp(prefix="sp5man_")

    def make_manifest(files, out_dir, combined=None):
        info = []
        for name in files:
            p = os.path.join(out_dir, name)
            ex = os.path.isfile(p)
            info.append({
                "name": name, "present": ex,
                "size_bytes": os.path.getsize(p) if ex else 0,
                "sha256": h5._sha256_file(p) if ex else None,
            })
        return {
            "manifest_version": "1.0", "handoff_id": "PKG-TEST-0001",
            "files": info,
            "integrity": {
                "algorithm": "sha256-16",
                "all_files_present": all(f["present"] for f in info),
                "combined_checksum": combined if combined is not None
                else h5._combined(info),
            },
        }

    # 正常场景：文件齐全 → 自检通过（manifest 自身完整性由封缄文件证明）
    for n in ("a.json", "b.json"):
        save_json({"k": n}, os.path.join(tmp, n))
    mp = os.path.join(tmp, "manifest.json")
    mf = make_manifest(["a.json", "b.json", "manifest.json"], tmp)
    # manifest 自身条目按约定不记录哈希；all_files_present 按内容文件计算
    for f in mf["files"]:
        if f["name"] == "manifest.json":
            f["present"] = True
            f["size_bytes"] = None
            f["sha256"] = None
    mf["integrity"]["all_files_present"] = True
    save_json(mf, mp)
    h5.write_seal(mp, tmp)
    v = h5.verify_manifest(mp, tmp)
    check("正常 manifest 自检通过", v["ok"] is True, str(v["problems"]))
    check("封缄文件记录 manifest 真实哈希",
          v["seal"] and v["seal"]["declared_sha256"] == v["seal"]["actual_sha256"],
          str(v.get("seal")))

    # 缺陷 A：声明存在但磁盘缺失（等价于原 fee_model.json 仅存在于 ZIP 内）
    mf = load_json(mp)
    for f in mf["files"]:
        if f["name"] == "b.json":
            f["present"] = True
            f["size_bytes"] = 123
            f["sha256"] = "deadbeefdeadbeef"
    mf["integrity"]["all_files_present"] = True
    os.remove(os.path.join(tmp, "b.json"))
    save_json(mf, mp)
    h5.write_seal(mp, tmp)
    v = h5.verify_manifest(mp, tmp)
    check("检出『声明存在但磁盘缺失』", v["ok"] is False
          and any("b.json" in p for p in v["problems"]), str(v["problems"]))
    check("检出 all_files_present 虚假声明",
          any("all_files_present" in p for p in v["problems"]), str(v["problems"]))

    # 缺陷 B：哈希/尺寸陈旧（等价于原 decision_report.md / portfolio.json 失配）
    save_json({"k": "b.json", "changed": True}, os.path.join(tmp, "b.json"))
    v = h5.verify_manifest(mp, tmp)
    check("检出哈希陈旧", v["ok"] is False
          and any("b.json" in p and "sha256" in p for p in v["problems"]),
          str(v["problems"]))
    check("检出尺寸陈旧", any("b.json" in p and "size" in p for p in v["problems"]),
          str(v["problems"]))

    # 缺陷 C：manifest 被改动但封缄未更新（等价于原 3236 字节僵尸块无法被发现）
    mf = load_json(mp)
    for name in ("a.json", "b.json"):
        p = os.path.join(tmp, name)
        for f in mf["files"]:
            if f["name"] == name:
                f["present"] = True
                f["size_bytes"] = os.path.getsize(p)
                f["sha256"] = h5._sha256_file(p)
    mf["integrity"]["all_files_present"] = True
    mf["integrity"]["combined_checksum"] = h5._combined(mf["files"])
    save_json(mf, mp)          # 故意不更新封缄
    v = h5.verify_manifest(mp, tmp)
    check("检出 manifest 被改动而封缄未更新", v["ok"] is False
          and any("封缄不符" in p for p in v["problems"]), str(v["problems"]))

    # 缺陷 C2：manifest 内部又去记录自身哈希（旧错误做法）必须被拒绝
    mf = load_json(mp)
    for f in mf["files"]:
        if f["name"] == "manifest.json":
            f["size_bytes"] = 3236            # 陈旧尺寸（v4 的值）
            f["sha256"] = "2f20202bc9f9f70c"  # 陈旧哈希
    save_json(mf, mp)
    h5.write_seal(mp, tmp)
    v = h5.verify_manifest(mp, tmp)
    check("拒绝 manifest 记录自身哈希（自指不可能自洽）",
          any("不得记录自身" in p for p in v["problems"]), str(v["problems"]))

    # 缺陷 D：combined_checksum 无法重算
    mf = load_json(mp)
    for f in mf["files"]:
        if f["name"] == "manifest.json":
            f["sha256"] = None
            f["size_bytes"] = None
    mf["integrity"]["combined_checksum"] = "0000000000000000"
    save_json(mf, mp)
    h5.write_seal(mp, tmp)
    v = h5.verify_manifest(mp, tmp)
    check("检出 combined_checksum 不可重算",
          any("combined_checksum" in p for p in v["problems"]), str(v["problems"]))

    # 缺陷 E：缺少封缄文件 → manifest 自身完整性不可验证
    os.remove(os.path.join(tmp, h5.SEAL_NAME))
    v = h5.verify_manifest(mp, tmp)
    check("缺少封缄文件被检出",
          any("封缄文件" in p for p in v["problems"]), str(v["problems"]))
    shutil.rmtree(tmp, ignore_errors=True)

    # ---------------- 2. 交付交接包的真实完整性 ----------------
    section("2. 交付交接包完整性（真实字节校验，非自我声明）")
    manifest_path = os.path.join(REPORTS, "manifest.json")
    check("交接包 manifest 存在", os.path.exists(manifest_path))
    v = h5.verify_manifest(manifest_path)
    check("manifest 自检通过（哈希/尺寸/组合校验和全部可重算）",
          v["ok"] is True, str(v["problems"]))

    m = v["manifest"]
    check("manifest 声明 13 个文件全部真实存在于磁盘",
          all(f["present"] and os.path.isfile(os.path.join(REPORTS, f["name"]))
              for f in m["files"]),
          str([f["name"] for f in m["files"]
               if not os.path.isfile(os.path.join(REPORTS, f["name"]))]))
    check("每个内容文件都有 sha256",
          all(f.get("sha256") for f in m["files"] if f["name"] != "manifest.json"))
    check("manifest 不记录自身哈希（自指不可能自洽）",
          all(not f.get("sha256") and f.get("self_reference") is False
              for f in m["files"] if f["name"] == "manifest.json"),
          str([f for f in m["files"] if f["name"] == "manifest.json"]))
    check("封缄文件与 manifest 落盘字节一致",
          v["seal"] is not None
          and v["seal"]["declared_sha256"] == h5._sha256_file(manifest_path)
          and v["seal"]["declared_size"] == os.path.getsize(manifest_path),
          str(v.get("seal")))

    # 逐内容文件与磁盘比对（原测试从未做过这一步）
    mism = []
    for f in m["files"]:
        if f["name"] == "manifest.json":
            continue
        p = os.path.join(REPORTS, f["name"])
        if h5._sha256_file(p) != f["sha256"] or os.path.getsize(p) != f["size_bytes"]:
            mism.append(f["name"])
    check("全部 12 个内容文件哈希与尺寸与磁盘一致", not mism, str(mism))

    recomputed = h5._combined(m["files"])
    check("combined_checksum 可用真实声明重算",
          recomputed == m["integrity"]["combined_checksum"],
          f"{recomputed} vs {m['integrity']['combined_checksum']}")

    # ---------------- 3. history_links 来自账本 ----------------
    section("3. 历史留痕来自账本（回归：原 hardcode 0）")
    preds = L.get_predictions(limit=1000)
    real_total = len(preds)
    real_resolved = sum(1 for p in preds if p.get("resolved"))
    hl = m["history_links"]
    check("predictions_total 与账本一致",
          hl["predictions_total"] == real_total,
          f"manifest={hl['predictions_total']} ledger={real_total}")
    check("predictions_resolved 与账本一致",
          hl["predictions_resolved"] == real_resolved,
          f"manifest={hl['predictions_resolved']} ledger={real_resolved}")
    check("decisions 与账本一致",
          hl["decisions"] == len(L.get_decisions(limit=1000)),
          f"manifest={hl['decisions']}")
    # 与包内 prediction_history.json 自洽
    ph = load_json(os.path.join(REPORTS, "prediction_history.json"))
    check("manifest 与包内 prediction_history.json 预测总数自洽",
          hl["predictions_total"] == len(ph.get("predictions", [])),
          f"{hl['predictions_total']} vs {len(ph.get('predictions', []))}")
    check("manifest 与包内 prediction_history.json 已结算数自洽",
          hl["predictions_resolved"] == ph["summary"].get("resolved_total"),
          f"{hl['predictions_resolved']} vs {ph['summary'].get('resolved_total')}")

    # ---------------- 4. package 指针 ----------------
    section("4. handoff_latest.json 指针不得悬空")
    lp = os.path.join(REPORTS, "handoff_latest.json")
    latest = load_json(lp)
    check("latest 指向的 package 本体真实存在",
          os.path.isfile(latest.get("package", "")), latest.get("package"))
    check("latest 指向的 zip 真实存在", os.path.isfile(latest.get("zip", "")))
    check("latest 的 handoff_id 与 manifest 一致",
          latest.get("latest_handoff_id") == m["handoff_id"])

    # ZIP 完整性：每个条目都必须与磁盘一致（manifest 用封缄校验）
    zpath = latest.get("zip")
    if zpath and os.path.isfile(zpath):
        z = zipfile.ZipFile(zpath)
        names = z.namelist()
        bad = []
        for f in m["files"]:
            arc = f"{m['handoff_id']}/{f['name']}"
            if arc not in names:
                bad.append((f["name"], "缺条目"))
                continue
            b = z.read(arc)
            disk = open(os.path.join(REPORTS, f["name"]), "rb").read()
            if b != disk:
                bad.append((f["name"], "与磁盘不一致"))
        z.close()
        check("ZIP 内每个条目都与磁盘一致（含 manifest 自身）", not bad, str(bad))
        check("ZIP 含封缄文件", any(n.endswith(h5.SEAL_NAME) for n in names))
        check("ZIP 含证据文件", any(n.endswith("evidence_source_health.json")
                                    for n in names))

    # ---------------- 5. 费率唯一来源 ----------------
    section("5. 费率唯一来源（回归：原 simulation.py 内联重复费率常量）")
    src = open(os.path.join(os.path.dirname(os.path.dirname(
        os.path.abspath(__file__))), "simulation.py"), encoding="utf-8").read()
    check("simulation.py 不再内联费率常量",
          "COMMISSION_RATE =" not in src and "STAMP_RATE =" not in src
          and "TRANSFER_RATE =" not in src)
    check("simulation.py 导入 fee_model", "import fee_model" in src)

    etf, cfg = sim.resolve_fee_config("600011", "华能国际", "2026-10-16")
    check("股票取到股票费率配置", etf is False and not cfg.is_etf, cfg.config_id)
    etf_e, cfg_e = sim.resolve_fee_config("510300", "沪深300ETF", "2026-10-16")
    check("ETF 取到 ETF 费率配置", etf_e is True and cfg_e.is_etf, cfg_e.config_id)
    check("ETF 配置免印花税", cfg_e.stamp_duty_rate_sell == 0.0)

    fb = fee_model.calc_fee_breakdown(7.59, 100, False, cfg, "buy")
    # 买入：佣金 5.0（触发最低佣金）+ 过户费 0.01 + 经手费/证管费 0.04 = 5.05
    check("模拟买入费用与 fee_model 一致（佣金5.0/费用5.05）",
          fb["commission"] == 5.0 and fb["total_fee"] == 5.05,
          f"{fb['commission']}/{fb['total_fee']}")
    check("买入含官方经手费+证管费（other_fee > 0）", fb["other_fee"] > 0,
          str(fb["other_fee"]))
    fs = fee_model.calc_fee_breakdown(7.59, 100, False, cfg, "sell")
    # 印花税现行 0.05%（2023-08-28 减半后），不是千分之一
    check("卖出印花税为现行 0.05%（非旧的 0.1%）",
          abs(fs["stamp_duty"] - round(759.0 * 0.0005, 2)) < 0.005
          and fs["stamp_duty"] != round(759.0 * 0.001, 2),
          f"stamp={fs['stamp_duty']}")
    fe = fee_model.calc_fee_breakdown(4.236, 100, True, cfg_e, "sell")
    check("模拟 ETF 卖出免印花税", fe["stamp_duty"] == 0.0)

    # calc_round_trip 不再硬编码 1000
    rt = fee_model.calc_round_trip(7.51, 100, False, cfg, initial_capital=500.0)
    check("calc_round_trip 支持自定义本金",
          rt["initial_capital"] == 500.0
          and abs(rt["pct_of_initial_capital"]
                  - round(rt["round_trip_total"] / 500.0 * 100, 3)) < 1e-6,
          str(rt["pct_of_initial_capital"]))
    rt_def = fee_model.calc_round_trip(7.51, 100, False, cfg)
    check("默认本金为 1000 且回本点按官方印花税计算",
          rt_def["pct_of_initial_capital"] == 1.047
          and rt_def["breakeven_pct"] == 1.394,
          f"{rt_def['pct_of_initial_capital']}%/{rt_def['breakeven_pct']}%")

    # batch_scenarios 不再访问私有属性
    fm_src = open(os.path.join(os.path.dirname(os.path.dirname(
        os.path.abspath(__file__))), "fee_model.py"), encoding="utf-8").read()
    check("batch_scenarios 使用公开 list_configs()", "manager._configs" not in fm_src)
    sc = fee_model.batch_scenarios(7.51, 100, False, fee_model.fee_manager, "2026-10-16")
    check("股票敏感性返回 3 档", len(sc) == 3, str(len(sc)))

    # ---------------- 6. 审计字段映射与严重度 ----------------
    section("6. 研究审计字段映射与严重度（回归：读错文件/字段）")
    news = load_json(os.path.join(REPORTS, "news_events.json"))
    ev0 = news["events"][0]
    check("新闻真实字段为 sec_code", "sec_code" in ev0)
    cands = load_json(os.path.join(REPORTS, "stock_candidates.json"))
    ccodes = {c["code"] for c in cands["candidates"]}
    m2 = ra.audit_news_reliability(os.path.join(REPORTS, "news_events.json"), ccodes)
    check("7.2 相关性使用真实字段（非全部 macro）",
          m2["relevance_stats"]["macro"] < m2["relevance_stats"]["total"],
          str(m2["relevance_stats"]))
    check("7.2 统计已关联标的代码",
          len(m2["relevance_stats"]["distinct_sec_codes"]) > 0,
          str(m2["relevance_stats"]["distinct_sec_codes"]))
    # 构造：把一个事件改成候选代码，相关性命中必须生效
    tmpn = os.path.join(tempfile.mkdtemp(prefix="sp5news_"), "n.json")
    fake = json.loads(json.dumps(news))
    fake["events"][0]["sec_code"] = "600011"
    save_json(fake, tmpn)
    m2b = ra.audit_news_reliability(tmpn, ccodes)
    check("7.2 候选命中被正确识别（原实现字段写错必漏）",
          m2b["relevance_stats"]["related_to_candidates"] >= 1,
          str(m2b["relevance_stats"]))
    shutil.rmtree(os.path.dirname(tmpn), ignore_errors=True)

    m3 = ra.audit_candidate_selection(
        os.path.join(REPORTS, "stock_candidates.json"),
        research_path=os.path.join(REPORTS, "research.json"))
    cov = m3["fundamentals_coverage"]
    check("7.3 财务数据覆盖率 10/10（原实现误报全部缺失）",
          cov["with_fundamentals"] == cov["candidates"] == 10, str(cov))
    check("7.3 费用信息覆盖率 10/10", cov["with_fee"] == 10, str(cov))
    check("7.3 不再误报『缺少财务数据』",
          not any("缺少财务数据" in i for r in m3["details"] for i in r["issues"]),
          str([i for r in m3["details"] for i in r["issues"] if "缺少财务数据" in i]))
    short = [r for r in m3["details"] if "短期涨幅过大" in " ".join(r["issues"])]
    check("7.3 短期涨幅警告为真实数量 5（原报告称 3）", len(short) == 5,
          f"actual={len(short)}")
    cost = [r for r in m3["details"] if "交易成本过高" in " ".join(r["issues"])]
    check("7.3 交易成本警告真实存在（原实现全为『缺少成本信息』）", len(cost) > 0,
          f"actual={len(cost)}")

    # warn 必须翻转 verdict
    probe = {"code": "600011", "name": "x", "price": 1.0,
             "checks": {}, "issues": [], "verdict": "pass"}
    ra._escalate(probe, "warn", "w")
    check("_escalate 使 warn 翻转 verdict", probe["verdict"] == "warn")
    ra._escalate(probe, "info", "i")
    check("_escalate 不会把 warn 下调为 info", probe["verdict"] == "warn")
    ra._escalate(probe, "fail", "f")
    check("_escalate 可升到 fail", probe["verdict"] == "fail")

    # 指数代码归一化 + 已知限制降级
    check("7.1 接受 sh000001 规范前缀代码",
          ra.normalize_code("sh000001") == "000001")
    check("7.1 接受纯数字代码", ra.normalize_code("600011") == "600011")
    check("7.1 拒绝非法代码", ra.normalize_code("abc") is None)
    m1 = ra.audit_market_quote(os.path.join(REPORTS, "market_snapshot.json"),
                               os.path.join(REPORTS, "quality_report.json"))
    check("7.1 指数不再被判 fail（已知限制降级为 warn）", m1["failed"] == 0,
          str([(r["code"], r["verdict"]) for r in m1["details"] if r["verdict"] == "fail"]))
    check("7.1 覆盖指数+股票+ETF",
          {r["kind"] for r in m1["details"]} == {"index", "stock", "etf"},
          str({r["kind"] for r in m1["details"]}))

    allres = ra.run_all_audits()
    check("审计总体不再为 fail", allres["summary"]["overall"] != "fail",
          str(allres["summary"]))
    check("审计 warned 统计非零（原实现恒为 0）",
          allres["summary"]["warned"] > 0, str(allres["summary"]["warned"]))
    check("审计 failed 为 0", allres["summary"]["failed"] == 0,
          str(allres["summary"]["failed"]))

    # ---------------- 7. 模拟成交引擎补齐 ----------------
    section("7. 模拟成交引擎（滑点 / 涨跌停 / sqlite3.Row 兼容）")
    check("主板涨跌停 10%", sim.detect_price_limit("600011", "华能国际") == 0.10)
    check("创业板涨跌停 20%", sim.detect_price_limit("300750", "宁德时代") == 0.20)
    check("科创板涨跌停 20%", sim.detect_price_limit("688111", "金山办公") == 0.20)
    check("北交所涨跌停 30%", sim.detect_price_limit("830799", "艾融软件") == 0.30)
    check("ST 涨跌停 5%", sim.detect_price_limit("600011", "ST华能") == 0.05)

    locked_bar = {"date": "2026-10-16", "high": 8.26, "low": 8.26}
    normal_bar = {"date": "2026-10-16", "high": 7.80, "low": 7.44}
    ok_lock, kind = sim.is_limit_locked(locked_bar, 7.51, 0.10)
    check("识别涨停一字板", ok_lock is True and kind == "涨停", str(kind))
    ok_norm, _ = sim.is_limit_locked(normal_bar, 7.51, 0.10)
    check("正常波动不误判为一字板", ok_norm is False)

    limit_order = {"action": "buy", "code": "600011", "name": "华能国际",
                   "price": 8.26, "shares": 100}
    st_lu, fp_lu, rule_lu = sim.evaluate_fill(limit_order, locked_bar, {}, prev_close=7.51)
    check("一字板当日不成交", st_lu == sim.STATUS_NOT_MET and fp_lu is None, rule_lu)

    check("买入滑点向上", sim.apply_slippage(10.0, "buy") > 10.0)
    check("卖出滑点向下", sim.apply_slippage(10.0, "sell") < 10.0)
    check("滑点可关闭", sim.apply_slippage(10.0, "buy", 0) == 10.0)
    check("滑点比例符合 SLIPPAGE_RATE",
          abs(sim.apply_slippage(10.0, "buy")
              - round(10.0 * (1 + sim.SLIPPAGE_RATE), 4)) < 1e-6)

    o_slip = {"action": "buy", "code": "600011", "name": "华能国际",
              "price": 7.59, "shares": 100}
    st_s, fp_s, _ = sim.evaluate_fill(o_slip, normal_bar, {})
    check("成交价已包含滑点（不再等于委托价）",
          st_s == sim.STATUS_ASSUMED_DAILY and fp_s > 7.59, str(fp_s))
    st_s0, fp_s0, _ = sim.evaluate_fill(o_slip, normal_bar, {}, slippage_rate=0)
    check("关闭滑点后成交价等于委托价", fp_s0 == 7.59, str(fp_s0))

    import sqlite3 as _sq
    con = _sq.connect(":memory:")
    con.row_factory = _sq.Row
    con.execute("CREATE TABLE t(action TEXT, code TEXT, name TEXT, price REAL, shares INT)")
    con.execute("INSERT INTO t VALUES('buy','600011','华能国际',7.59,100)")
    row = con.execute("SELECT * FROM t").fetchone()
    st_r, fp_r, _ = sim.evaluate_fill(row, normal_bar, {}, slippage_rate=0)
    check("evaluate_fill 兼容 sqlite3.Row",
          st_r == sim.STATUS_ASSUMED_DAILY and fp_r == 7.59, str(fp_r))
    con.close()

    # ---------------- 8. 调度器 ----------------
    section("8. 调度器（时段判断 / 不伪造实时数据）")
    from stockpilot import scheduler as sch
    # 交易时段内应拒绝运行
    thu_am = dt.datetime(2026, 10, 22, 10, 30)   # 周四上午盘中
    ok1, r1 = sch.should_run(now=thu_am)
    check("盘中拒绝运行（避免半截数据）", ok1 is False and "交易时段" in r1, r1)
    # 收盘后应允许
    thu_pm = dt.datetime(2026, 10, 22, 15, 30)
    ok2, r2 = sch.should_run(now=thu_pm)
    check("收盘后允许运行", ok2 is True, r2)
    # 非交易日允许但明确标注
    sat = dt.datetime(2026, 10, 24, 15, 30)      # 周六
    ok3, r3 = sch.should_run(now=sat)
    check("非交易日允许运行但标注数据陈旧", ok3 is True and "最近有效交易日" in r3, r3)
    # force 覆盖
    ok4, r4 = sch.should_run(now=thu_am, force=True)
    check("--force 可强制运行", ok4 is True, r4)
    # 交易时段判定
    check("09:30-11:30 为交易时段",
          sch.in_trading_session(dt.datetime(2026, 10, 22, 10, 0)) is True)
    check("午休非交易时段",
          sch.in_trading_session(dt.datetime(2026, 10, 22, 12, 0)) is False)
    check("周末非交易时段",
          sch.in_trading_session(dt.datetime(2026, 10, 24, 10, 0)) is False)
    # 不得自动注册系统任务：源码中不应出现任务计划注册调用
    sch_src = open(os.path.join(os.path.dirname(os.path.dirname(
        os.path.abspath(__file__))), "scheduler.py"), encoding="utf-8").read()
    check("调度器不注册系统任务计划",
          "schtasks" not in sch_src.lower()
          and "Register-ScheduledTask" not in sch_src
          and "win32com" not in sch_src)

    # ---------------- 9. 交接包本体校验和 ----------------
    section("9. 交接包本体 checksum（回归：追加字段后未重算）")
    lp2 = os.path.join(REPORTS, "handoff_latest.json")
    if os.path.isfile(lp2):
        latest2 = load_json(lp2)
        pkg_path = latest2.get("package")
        check("包本体存在", bool(pkg_path) and os.path.isfile(pkg_path), str(pkg_path))
        if pkg_path and os.path.isfile(pkg_path):
            pkg = load_json(pkg_path)
            declared = pkg.get("integrity", {}).get("checksum")
            recomputed = h5.package_checksum(pkg)
            check("包本体 checksum 可用全部字段重算",
                  declared == recomputed, f"{declared} vs {recomputed}")
            check("包本体含 v5_manifest_checksum 且与 manifest 一致",
                  pkg.get("v5_manifest_checksum")
                  == m["integrity"]["combined_checksum"],
                  f"{pkg.get('v5_manifest_checksum')} vs "
                  f"{m['integrity']['combined_checksum']}")
            # 追加字段后必须重算：若把某个字段改掉，checksum 应当失配
            tampered = json.loads(json.dumps(pkg))
            tampered["handoff_id"] = "PKG-TAMPERED-000000"
            check("篡改包本体后 checksum 失配（校验有效）",
                  h5.package_checksum(tampered) != declared)

    # ---------------- 10. 两套费用实现必须一致 ----------------
    section("10. 账本费用与 fee_model 一致性（回归：双份实现漂移）")
    from stockpilot import ledger as L2
    cfg_s = [c for c in fee_model.fee_manager.list_configs()
             if c.config_id == "stock_pessimistic_5yuan"][0]
    for direction in ("buy", "sell"):
        lf = L2.calc_fees(7.51, 100, is_etf=False, direction=direction)
        ff = fee_model.calc_fee_breakdown(7.51, 100, False, cfg_s, direction)
        check(f"账本与 fee_model 在 {direction} 方向总额一致",
              abs(lf["total_fee"] - ff["total_fee"]) <= 0.02,
              f"ledger={lf['total_fee']} fee_model={ff['total_fee']}")
        check(f"账本与 fee_model 在 {direction} 方向印花税一致",
              abs(lf["stamp_duty"] - ff["stamp_duty"]) <= 0.01,
              f"ledger={lf['stamp_duty']} fee_model={ff['stamp_duty']}")

    # 买入不得收印花税（关键回归：早期实现买卖都收）
    lb = L2.calc_fees(7.51, 100, is_etf=False, direction="buy")
    ls = L2.calc_fees(7.51, 100, is_etf=False, direction="sell")
    check("账本买入不收印花税", lb["stamp_duty"] == 0.0, str(lb["stamp_duty"]))
    check("账本卖出收印花税 0.05%",
          abs(ls["stamp_duty"] - round(751.0 * 0.0005, 2)) < 0.01,
          str(ls["stamp_duty"]))
    check("账本买入含经手费+证管费",
          lb["handling_fee"] > 0 and lb["supervision_fee"] > 0, str(lb))
    # ETF 卖出免印花税
    le = L2.calc_fees(4.236, 100, is_etf=True, direction="sell")
    check("账本 ETF 卖出免印花税", le["stamp_duty"] == 0.0)

    # package 的全局 FEE_MODEL 也必须用新税率
    from stockpilot import FEE_MODEL
    check("全局 FEE_MODEL 印花税为 0.0005（非旧 0.001）",
          FEE_MODEL["stamp_duty_rate"] == 0.0005,
          str(FEE_MODEL["stamp_duty_rate"]))

    # ---------------- 11. 代码卫生 ----------------
    section("11. 代码卫生（回归：重复 import/print）")
    fm_src = open(os.path.join(os.path.dirname(os.path.dirname(
        os.path.abspath(__file__))), "fee_model.py"), encoding="utf-8").read()
    check("fee_model.py 无重复 import json", fm_src.count("import json") == 1,
          str(fm_src.count("import json")))
    check("fee_model.py 自测无重复输出",
          fm_src.count('print(json.dumps(rt') == 1,
          str(fm_src.count('print(json.dumps(rt')))
    ra_src = open(os.path.join(os.path.dirname(os.path.dirname(
        os.path.abspath(__file__))), "research_audit.py"), encoding="utf-8").read()
    check("research_audit.py 使用绝对路径（不依赖 CWD）",
          '"reports/market_snapshot.json"' not in ra_src)
    h5_src = open(os.path.join(os.path.dirname(os.path.dirname(
        os.path.abspath(__file__))), "handoff_v5.py"), encoding="utf-8").read()
    check("handoff_v5.py 不再硬编码 prediction_total: 0",
          '"prediction_total": 0' not in h5_src)
    check("handoff_v5.py 导出前执行 verify_manifest 硬校验",
          "verify_manifest(manifest_path" in h5_src)
    check("handoff_v5.py 生成封缄文件", "write_seal(manifest_path" in h5_src)
    check("handoff_v5.py 将预测计数取自账本",
          "len(preds)" in h5_src and "get_predictions" in h5_src)

    print(f"\n{'='*50}")
    print(f"总计: {len(PASS)} 通过, {len(FAIL)} 失败")
    if FAIL:
        for n, d in FAIL:
            print(f"  FAIL: {n} {d}")
        sys.exit(1)
    print("全部测试通过")


if __name__ == "__main__":
    main()
