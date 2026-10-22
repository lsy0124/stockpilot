"""StockPilot 账本与时间管理测试套件（第三阶段新增）。

对应任务书第十六章。全部使用独立测试数据库（temp 目录），
绝不触碰 data/stockpilot.db 真实账本。

运行：
    python -m stockpilot.tests.test_ledger
    python -m stockpilot.tests.test_ledger -v      # 详细输出
"""
import datetime as dt
import json
import os
import shutil
import sys
import tempfile

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
    import stockpilot.ledger as L
    tmp = tempfile.mkdtemp(prefix="stockpilot_test_")
    db = os.path.join(tmp, "test.db")
    try:
        L.init_db(db)
        con = L.connect(db)

        # ---------------- 1. 初始状态 ----------------
        section("1. 初始账户")
        pf = L.portfolio(path=db)
        check("初始资金1000", pf["initial_capital"] == 1000.0)
        check("初始现金1000", pf["cash"] == 1000.0)
        check("初始零持仓", pf["positions"] == [])
        check("初始零成交", pf["trade_count"] == 0)
        check("初始对账通过", L.reconcile(path=db)["ok"])

        # ---------------- 2. 时间戳与跨交易日 ----------------
        section("2. 时间戳与时区")
        from stockpilot import timeutil as tu
        now = tu.now()
        check("当前时间带时区", now.tzinfo is not None and now.utcoffset().total_seconds() == 8 * 3600)
        f = tu.freshness(dt.datetime(2026, 10, 16, 16, 12), dt.datetime(2026, 10, 18, 13, 0))
        check("周末数据不算过期", f["is_stale"] is False)
        check("周末提示正常收盘数据", "正常收盘数据" in f["reason"])
        f2 = tu.freshness(dt.datetime(2026, 10, 16, 10, 0), dt.datetime(2026, 10, 16, 10, 40))
        check("交易时段超时判定过期", f2["is_stale"] is True)
        f3 = tu.freshness(dt.datetime(2026, 10, 16, 10, 0), dt.datetime(2026, 10, 16, 10, 5))
        check("交易时段新鲜数据", f3["is_stale"] is False)
        lbl = tu.trading_day_label(dt.date(2026, 10, 16), dt.datetime(2026, 10, 18, 13, 0))
        check("非交易日标签含陈旧天数", "陈旧 2 天" in lbl, lbl)

        # ---------------- 3. 买入流程与费用 ----------------
        section("3. 买入、费用与现金")
        a = L.add_advice("GLM", "buy", code="600028", name="中国石化", market="A",
                         price_low=5.3, price_high=5.6, shares=100,
                         reason_bull="站上MA20", reason_bear="成本偏高", path=db)
        i = L.add_intent(a, "buy", "600028", "中国石化", "A", price=5.5, shares=100, path=db)
        o = L.submit_order(i, "buy", "600028", "中国石化", "A", price=5.5, shares=100, path=db)
        r = L.confirm_trade(o, price=5.5, shares=100, trade_uid="T-001", path=db)
        check("买入成交返回id", r["id"] > 0 and not r["duplicate"])
        pf = L.portfolio(path=db)
        check("买入后现金减少", pf["cash"] == 1000 - 550 - r["fees"]["total_fee"],
              f"cash={pf['cash']} fees={r['fees']}")
        check("买入有持仓", len(pf["positions"]) == 1 and pf["positions"][0]["shares"] == 100)
        check("佣金按最低5元", r["fees"]["commission"] == 5.0, str(r["fees"]))
        # 印花税只在卖出收取；买入时必须为 0（早期实现错误地在买入计入）
        check("买入不收印花税", r["fees"]["stamp_duty"] == 0.0, str(r["fees"]))
        # 总费用 = 佣金 + 印花 + 过户 + 经手费 + 证管费
        check("总费用=佣金+印花+过户+经手费+证管费",
              r["fees"]["total_fee"] == round(
                  r["fees"]["commission"] + r["fees"]["stamp_duty"]
                  + r["fees"]["transfer_fee"] + r["fees"]["other_fee"], 2),
              str(r["fees"]))
        check("买入含官方经手费与证管费",
              r["fees"]["handling_fee"] > 0 and r["fees"]["supervision_fee"] > 0,
              str(r["fees"]))
        check("买入后对账通过", L.reconcile(path=db)["ok"])

        # ---------------- 4. 幂等：重复导入同一成交 ----------------
        section("4. 幂等与重复导入")
        r2 = L.confirm_trade(o, price=5.5, shares=100, trade_uid="T-001", path=db)
        check("重复成交被忽略", r2["duplicate"] is True)
        pf2 = L.portfolio(path=db)
        check("重复成交不改变持仓", pf2["positions"][0]["shares"] == 100)
        check("重复成交不改变现金", pf2["cash"] == pf["cash"])
        check("重复成交不增加笔数", pf2["trade_count"] == 1)

        # ---------------- 5. 资金不足 ----------------
        section("5. 资金不足拒绝")
        a2 = L.add_advice("GLM", "buy", code="600519", name="贵州茅台", market="A", path=db)
        i2 = L.add_intent(a2, "buy", "600519", "贵州茅台", "A", price=1300, shares=100, path=db)
        o2 = L.submit_order(i2, "buy", "600519", "贵州茅台", "A", price=1300, shares=100, path=db)
        try:
            L.confirm_trade(o2, price=1300, shares=100, trade_uid="T-002", path=db)
            check("资金不足应抛异常", False)
        except ValueError as e:
            check("资金不足被拒绝", "资金不足" in str(e))
        check("拒绝后现金不变", L.portfolio(path=db)["cash"] == pf["cash"])
        check("拒绝后无茅台持仓", all(p["code"] != "600519" for p in L.positions(path=db)))

        # ---------------- 6. 部分成交 ----------------
        section("6. 部分成交")
        a3 = L.add_advice("GLM", "buy", code="601398", name="工商银行", market="A", path=db)
        i3 = L.add_intent(a3, "buy", "601398", "工商银行", "A", price=2.0, shares=100, path=db)
        o3 = L.submit_order(i3, "buy", "601398", "工商银行", "A", price=2.0, shares=100, path=db)
        r3a = L.confirm_trade(o3, price=2.0, shares=50, trade_uid="T-003a", is_partial=1, path=db)
        con3 = L.connect(db)
        st = con3.execute("SELECT status, filled_shares FROM orders WHERE id=?", (o3,)).fetchone()
        con3.close()
        check("部分成交后状态partial", st["status"] == "partial", st["status"])
        check("已成交数量记录", st["filled_shares"] == 50)
        # 超过委托剩余数量应拒绝
        try:
            L.confirm_trade(o3, price=2.0, shares=60, trade_uid="T-003b", path=db)
            check("超过剩余数量应拒绝", False)
        except ValueError as e:
            check("超过剩余数量被拒绝", "超过委托剩余" in str(e))
        r3b = L.confirm_trade(o3, price=2.0, shares=50, trade_uid="T-003b", path=db)
        con3 = L.connect(db)
        st = con3.execute("SELECT status, filled_shares FROM orders WHERE id=?", (o3,)).fetchone()
        con3.close()
        check("成交完毕状态filled", st["status"] == "filled")
        check("两次部分成交合并持仓",
              any(p["code"]=="601398" and p["shares"]==100 for p in L.positions(path=db)))

        # ---------------- 7. 撤单 ----------------
        section("7. 撤单")
        a4 = L.add_advice("GLM", "buy", code="600019", name="宝钢股份", market="A", path=db)
        i4 = L.add_intent(a4, "buy", "600019", "宝钢股份", "A", price=6.0, shares=100, path=db)
        o4 = L.submit_order(i4, "buy", "600019", "宝钢股份", "A", price=6.0, shares=100, path=db)
        L.cancel_order(o4, path=db)
        try:
            L.confirm_trade(o4, price=6.0, shares=100, trade_uid="T-004", path=db)
            check("已撤委托不能成交", False)
        except ValueError as e:
            check("已撤委托成交被拒绝", "cancelled" in str(e))
        try:
            L.cancel_order(o4, path=db)
            check("重复撤单应拒绝", False)
        except ValueError as e:
            check("重复撤单被拒绝", "不可撤销" in str(e))

        # ---------------- 8. 卖出与盈亏 ----------------
        section("8. 卖出、盈亏与手续费")
        cash_before = L.portfolio(path=db)["cash"]
        a5 = L.add_advice("GLM", "sell", code="600028", name="中国石化", market="A", path=db)
        i5 = L.add_intent(a5, "sell", "600028", "中国石化", "A", price=6.0, shares=100, path=db)
        o5 = L.submit_order(i5, "sell", "600028", "中国石化", "A", price=6.0, shares=100, path=db)
        r5 = L.confirm_trade(o5, price=6.0, shares=100, trade_uid="T-005", path=db)
        pf5 = L.portfolio(path=db)
        check("卖出后无该持仓", all(p["code"] != "600028" for p in pf5["positions"]))
        check("卖出回款正确", abs(pf5["cash"] - (cash_before + 600 - r5["fees"]["total_fee"])) < 0.01)
        check("卖出对账通过", L.reconcile(path=db)["ok"])
        # 持仓不足卖出
        a6 = L.add_advice("GLM", "sell", code="600028", name="中国石化", market="A", path=db)
        i6 = L.add_intent(a6, "sell", "600028", "中国石化", "A", price=6.0, shares=100, path=db)
        o6 = L.submit_order(i6, "sell", "600028", "中国石化", "A", price=6.0, shares=100, path=db)
        try:
            L.confirm_trade(o6, price=6.0, shares=100, trade_uid="T-006", path=db)
            check("持仓不足应拒绝", False)
        except ValueError as e:
            check("持仓不足卖出被拒绝", "持仓不足" in str(e))

        # ---------------- 9. 分红 ----------------
        section("9. 分红记录")
        d = L.record_dividend("2026-06-30", "601398", "工商银行", "A", cash_per_share=0.15, path=db)
        check("分红按持仓入账", d["cash_in"] == round(0.15 * 100, 2), str(d))
        check("分红后对账通过", L.reconcile(path=db)["ok"])

        # ---------------- 10. ETF 规则 ----------------
        section("10. ETF 费用规则")
        a7 = L.add_advice("GLM", "buy", code="510300", name="沪深300ETF", market="ETF", path=db)
        i7 = L.add_intent(a7, "buy", "510300", "沪深300ETF", "ETF", price=4.2, shares=200, path=db)
        o7 = L.submit_order(i7, "buy", "510300", "沪深300ETF", "ETF", price=4.2, shares=200, path=db)
        r7 = L.confirm_trade(o7, price=4.2, shares=100, trade_uid="T-007", path=db)
        check("ETF免印花税", r7["fees"]["stamp_duty"] == 0.0, str(r7["fees"]))
        check("ETF仍有佣金", r7["fees"]["commission"] == 5.0)

        # ---------------- 11. 备份与恢复 ----------------
        section("11. 备份与恢复")
        bkdir = os.path.join(tmp, "backups")
        bk = L.backup(db, backup_dir=bkdir, keep=3)
        check("备份文件存在", os.path.exists(bk))
        check("备份完整性", L.integrity_check(bk))
        # 修改后再恢复
        pf_before = L.portfolio(path=db)
        a8 = L.add_advice("GLM", "buy", code="601398", name="工商银行", market="A", path=db)
        i8 = L.add_intent(a8, "buy", "601398", "工商银行", "A", price=2.0, shares=100, path=db)
        o8 = L.submit_order(i8, "buy", "601398", "工商银行", "A", price=2.0, shares=100, path=db)
        L.confirm_trade(o8, price=2.0, shares=100, trade_uid="T-008", path=db)
        check("恢复前有新成交", L.portfolio(path=db)["trade_count"] == pf_before["trade_count"] + 1)
        res = L.restore(bk, target=db)
        pf_after = L.portfolio(path=db)
        check("恢复后成交数回到备份点", pf_after["trade_count"] == pf_before["trade_count"],
              f"{pf_after['trade_count']} vs {pf_before['trade_count']}")
        check("恢复后现金一致", abs(pf_after["cash"] - pf_before["cash"]) < 0.01)
        check("恢复后对账通过", L.reconcile(path=db)["ok"])
        check("恢复保留了安全备份", os.path.exists(res["safety_backup"]))

        # ---------------- 12. 重复执行与恢复 ----------------
        section("12. 重复执行/意外中断恢复")
        # 重复 init_db 不应重复计入初始资金
        L.init_db(db)
        pf_r = L.portfolio(path=db)
        check("重复init不重复入金", pf_r["initial_capital"] == 1000.0 and
              pf_r["cash"] == pf_after["cash"], f"cash={pf_r['cash']}")
        # 恢复后：备份点之后的委托已不存在（符合预期），重复 confirm 同一 uid 仍幂等
        try:
            r_dup = L.confirm_trade(o8, price=2.0, shares=100, trade_uid="T-008", path=db)
            check("恢复后旧委托已不存在", False, "未抛异常")
        except ValueError as e:
            check("恢复后旧委托已不存在", "不存在" in str(e))
        # 恢复后重复导入恢复点之前已有的成交 uid 仍应幂等
        r_dup2 = L.confirm_trade(o, price=5.5, shares=100, trade_uid="T-001", path=db)
        check("恢复后重复成交仍幂等", r_dup2["duplicate"] is True)

        # ---------------- 13. 决策与预测不可篡改 ----------------
        section("13. 决策与预测留痕")
        did = L.add_decision(source="GPT-6", code="600028", name="中国石化",
                             direction="buy", price_low=5.3, price_high=5.6, shares=100,
                             horizon="1个月", reason_bull="站上MA20",
                             reason_bear="成本1.4%",
                             risk_condition="跌破MA20", exit_condition="回撤5%",
                             data_version="PKG-TEST-001", glm_research_ref="research.json", path=db)
        check("决策已记录", did > 0)
        pid = L.add_prediction("600028", "一周内上涨3%", horizon="5个交易日",
                               code="600028", base_price=5.5, path=db)
        # 尝试直接改写（模拟篡改）后检查新版本机制
        con4 = L.connect(db)
        n_before = con4.execute("SELECT COUNT(*) AS c FROM decisions").fetchone()["c"]
        con4.close()
        check("决策记录存在", n_before >= 1)
        # 修订必须建新版本
        did2 = L.add_decision(source="GPT-6", code="600028", name="中国石化",
                              direction="hold", price_low=None, price_high=None, shares=None,
                              horizon="1个月", reason_bull="维持",
                              reason_bear="等待", risk_condition="", exit_condition="",
                              data_version="PKG-TEST-002", glm_research_ref="research.json",
                              supersedes=did, path=db)
        con5 = L.connect(db)
        orig = con5.execute("SELECT * FROM decisions WHERE id=?", (did,)).fetchone()
        con5.close()
        check("原决策未被修改", orig["direction"] == "buy")
        check("修订产生新记录", did2 != did)
        audit_n = L.connect(db).execute(
            "SELECT COUNT(*) AS c FROM audit_log WHERE event='decision_recorded'").fetchone()["c"]
        check("决策有审计留痕", audit_n >= 2)

        # ---------------- 14. 数据源健康 ----------------
        section("14. 数据源故障处理")
        from stockpilot import source_health as sh
        h = sh.SourceHealth(path=os.path.join(tmp, "health.json"))
        calls = [0]
        def flaky():
            calls[0] += 1
            if calls[0] < 2:
                raise TimeoutError("timed out")
            return "ok"
        r, e = sh.with_retry(flaky, retries=2, backoff=0.05, label="flaky")
        check("可重试错误会重试并成功", r == "ok" and e is None)
        def waf():
            raise IOError("501 waf")
        r2h, e2h = sh.with_retry(waf, retries=3, backoff=0.05)
        check("WAF硬失败不重试", r2h is None and e2h is not None)
        for _ in range(3):
            h.record_fail("test.src", "connection aborted")
        check("连续失败打开熔断", h.is_available("test.src") is False)
        h.record_ok("test.src")
        check("恢复后熔断关闭", h.is_available("test.src") is True)

        con.close()
    finally:
        shutil.rmtree(tmp, ignore_errors=True)

    # ---------------- 汇总 ----------------
    print(f"\n{'='*50}")
    print(f"总计: {len(PASS)} 通过, {len(FAIL)} 失败")
    if FAIL:
        for n, d in FAIL:
            print(f"  FAIL: {n} {d}")
        sys.exit(1)
    print("全部测试通过")


if __name__ == "__main__":
    main()
