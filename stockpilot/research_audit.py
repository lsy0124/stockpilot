"""研究质量审计模块（第五阶段新增，任务书第七章）。

对研究报告进行独立质量审计：

7.1 行情准确性：指数 + 有代表性的股票/ETF，检查代码、名称、交易日期、价格、
    成交量、复权方式、数据源、数据延迟。
7.2 新闻可靠性：检查原始来源、发布时间、可靠性标记、与候选股票的相关性。
7.3 候选股票审计：ST/退市、权限限制、财务风险、短期涨幅过度依赖、交易成本低估。
7.4 独立反方审查：汇总反方审查的检查项与判定。

所有审计结果必须保留，错误案例不得掩盖。

修复记录（第五阶段复核）：
- 7.1 指数代码为新浪规范前缀形式（sh000001），原实现按"6位纯数字"判 fail，
  导致总体被拉成 fail。现按真实规范校验，并把"新浪不返回行情时刻"这一
  已知限制记为 warn（含说明），而非 fail。
- 7.2 新闻事件真实字段是 sec_code（不是 code），原实现字段名写错，
  相关性检查恒命中"未关联"。已按真实 schema 读取并兼容别名。
- 7.3 原实现只读 stock_candidates.json，而该文件的候选不含 fundamentals/fee
  （这两项在 research.json 中）。于是每只候选都被误判"缺少财务数据"和
  "缺少交易成本信息"，制造了一个并不存在的数据缺口。现合并读取 research.json。
- 7.3/7.1 warn 级问题原实现从不翻转 verdict，而汇总又按 verdict 统计 warned，
  导致 warned 恒为 0。现已修正。
"""
import datetime as dt
import json
import os
import re
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from stockpilot import REPORTS, audit, iso, save_json, setup_logger

log = setup_logger("research_audit", "research_audit.log")

# 严重程度排序：fail > warn > info > pass
_SEVERITY = {"pass": 0, "info": 1, "warn": 2, "fail": 3}


def load_json(path):
    with open(path, "r", encoding="utf-8") as f:
        return json.load(f)


def _escalate(result, verdict, issue=None):
    """把结果判定升到更高的严重程度（永不下调），并记录问题描述。

    这修正了原实现"warn 只记 issue 不改 verdict"的缺陷。
    """
    if issue:
        result["issues"].append(issue)
    cur = result.get("verdict", "pass")
    if _SEVERITY.get(verdict, 0) > _SEVERITY.get(cur, 0):
        result["verdict"] = verdict


def _module_summary(module, results):
    """按 verdict 统计模块结果（warn/info 分别计数，fail 单独计数）。"""
    counts = {"pass": 0, "info": 0, "warn": 0, "fail": 0}
    for r in results:
        counts[r.get("verdict", "pass")] = counts.get(r.get("verdict", "pass"), 0) + 1
    return {
        "module": module,
        "total": len(results),
        "passed": counts["pass"],
        "informed": counts["info"],
        "warned": counts["warn"],
        "failed": counts["fail"],
        "details": results,
    }


# ---------------------------------------------------------------- 7.1
# A 股证券代码规范：6 位数字，可带 sh/sz/bj 前缀（新浪/腾讯接口的规范形式）。
_CODE_RE = re.compile(r"^(?:(?:sh|sz|bj))?(\d{6})$", re.I)


def normalize_code(code):
    """归一化证券代码：返回 6 位数字部分，无法识别则返回 None。"""
    if not code:
        return None
    m = _CODE_RE.match(str(code).strip())
    return m.group(1) if m else None


def audit_market_quote(snapshot_path, quality_path=None):
    """7.1 行情准确性审计（指数 + 有代表性的股票/ETF）。

    检查：代码、名称、交易日期、价格、成交量、数据源、跨源一致性。
    """
    snap = load_json(snapshot_path)
    indices = snap.get("indices", [])
    results = []

    for idx in indices:
        code = idx.get("code", "")
        name = idx.get("name", "")
        close = idx.get("close")
        volume = idx.get("volume")
        quote_time = idx.get("time")
        source = idx.get("source", "")

        result = {"code": code, "name": name, "kind": "index",
                  "checks": {}, "issues": [], "verdict": "pass"}

        # 代码格式：接受 6 位数字或带 sh/sz/bj 前缀的规范形式
        norm = normalize_code(code)
        if norm:
            result["checks"]["code_format"] = "pass"
            result["normalized_code"] = norm
            if norm != str(code):
                result["code_format_note"] = (
                    f"接口原始形态为 {code}（行情接口规范前缀形式），"
                    f"已归一化为 {norm}")
        else:
            result["checks"]["code_format"] = "warn"
            result["normalized_code"] = None
            _escalate(result, "warn", f"代码格式无法归一化: {code}")

        if name:
            result["checks"]["name"] = "pass"
        else:
            _escalate(result, "fail", "名称缺失")
            result["checks"]["name"] = "fail"

        if close is not None and close > 0:
            result["checks"]["price"] = "pass"
            result["price"] = close
        else:
            _escalate(result, "fail", "价格缺失或异常")
            result["checks"]["price"] = "fail"

        # 交易日期：新浪指数接口不返回行情时刻（已知并已在交接包披露）
        if quote_time:
            result["checks"]["trade_date"] = "pass"
            result["trade_date"] = quote_time
        else:
            result["checks"]["trade_date"] = "warn"
            _escalate(result, "warn",
                      "行情时刻缺失：新浪指数接口不返回报价时间，"
                      "已知限制，已用采集时间近似并在交接包标注")

        if volume is not None:
            result["checks"]["volume"] = "pass"
            result["volume"] = volume
        else:
            result["checks"]["volume"] = "warn"
            _escalate(result, "warn", "成交量缺失")

        if source:
            result["checks"]["data_source"] = "pass"
            result["data_source"] = source
        else:
            result["checks"]["data_source"] = "warn"
            _escalate(result, "warn", "数据源未标记，单源风险")

        results.append(result)

    # 有代表性的股票/ETF：取质量报告里的跨源核验明细
    cross = []
    if quality_path and os.path.exists(quality_path):
        q = load_json(quality_path)
        cross = (q.get("checks", {}).get("cross_price", {}) or {}).get("detail", []) or []
    for item in cross:
        code = item.get("code", "")
        sources = item.get("sources", {}) or {}
        verdict = item.get("verdict", "warn")
        result = {"code": code, "name": item.get("name"),
                  "kind": "etf" if str(code).startswith(("5", "15", "16")) else "stock",
                  "checks": {}, "issues": [], "verdict": "pass"}

        norm = normalize_code(code)
        if norm:
            result["checks"]["code_format"] = "pass"
            result["normalized_code"] = norm
        else:
            result["checks"]["code_format"] = "fail"
            _escalate(result, "fail", f"代码格式异常: {code}")

        # 跨源价格一致性
        closes = {k: v.get("close") for k, v in sources.items() if v.get("close") is not None}
        if len(closes) >= 2:
            vals = list(closes.values())
            spread = (max(vals) - min(vals)) / max(vals) * 100 if max(vals) else 0.0
            result["cross_sources"] = closes
            result["spread_pct"] = round(spread, 4)
            if spread > 0.5:
                result["checks"]["cross_price"] = "warn"
                _escalate(result, "warn", f"跨源价格分歧 {spread:.4f}% > 0.5%")
            else:
                result["checks"]["cross_price"] = "pass"
        else:
            result["checks"]["cross_price"] = "warn"
            _escalate(result, "warn", f"跨源核验不足 2 个来源，存在单源风险")

        # 复权方式：腾讯返回 qfq，新浪日线为不复权（不得混用）
        adjusts = {v.get("adjust") for v in sources.values() if v.get("adjust")}
        result["adjust_modes"] = sorted(adjusts)
        if len(adjusts) > 1:
            result["checks"]["adjust_consistency"] = "warn"
            _escalate(result, "warn",
                      f"复权方式不一致 {sorted(adjusts)}，跨源比较需注意除权跳空")

        if verdict != "pass":
            _escalate(result, "warn", f"质量报告原判定: {verdict}")

        results.append(result)

    return _module_summary("7.1 行情准确性", results)


# ---------------------------------------------------------------- 7.2
def audit_news_reliability(news_path, candidate_codes):
    """7.2 新闻可靠性审计。

    真实事件 schema 字段名为 sec_code（兼容 code 别名）。
    检查：标题、发布时间、来源、可靠性标记、与候选股票的相关性。
    """
    news = load_json(news_path)
    events = news.get("events", [])
    results = []

    for e in events:
        # sec_code 为真实字段；code 为历史别名
        sec_code = e.get("sec_code") or e.get("code")
        sec_name = e.get("sec_name") or e.get("name")
        result = {
            "event_id": e.get("event_id"),
            "title": (e.get("title") or "")[:80],
            "sec_code": sec_code,
            "sec_name": sec_name,
            "type": e.get("type"),
            "published_at": e.get("published_at"),
            "source": e.get("source"),
            "source_type": e.get("source_type"),
            "reliability": e.get("reliability"),
            "url": e.get("url"),
            "checks": {},
            "issues": [],
            "verdict": "pass",
        }

        if e.get("title"):
            result["checks"]["title"] = "pass"
        else:
            result["checks"]["title"] = "fail"
            _escalate(result, "fail", "标题缺失")

        if e.get("published_at"):
            result["checks"]["published_at"] = "pass"
        else:
            result["checks"]["published_at"] = "fail"
            _escalate(result, "fail", "发布时间缺失")

        if e.get("source"):
            result["checks"]["source"] = "pass"
        else:
            result["checks"]["source"] = "fail"
            _escalate(result, "fail", "来源缺失")

        if e.get("reliability"):
            result["checks"]["reliability_marked"] = "pass"
        else:
            result["checks"]["reliability_marked"] = "warn"
            _escalate(result, "warn", "可靠性未标记")

        # 原始链接：官方公告必须可追溯
        if e.get("url"):
            result["checks"]["source_url"] = "pass"
        else:
            result["checks"]["source_url"] = "warn"
            _escalate(result, "warn", "缺少原始链接，不可追溯")

        # 相关性：用真实字段 sec_code 判断
        if sec_code and sec_code in candidate_codes:
            result["checks"]["relevance"] = "pass"
            result["relevance"] = "candidate"
        else:
            # info 级：宏观新闻不直接关联候选，不构成缺陷
            result["checks"]["relevance"] = "info"
            result["relevance"] = "macro" if not sec_code else "non_candidate"
            result["issues"].append(
                "未直接关联候选股票"
                + (f"（关联标的 {sec_code} {sec_name or ''}）" if sec_code else "（宏观新闻）"))

        results.append(result)

    summary = _module_summary("7.2 新闻可靠性", results)
    summary["details"] = results[:20]
    # 相关性统计（用真实字段，可复核）
    summary["relevance_stats"] = {
        "total": len(results),
        "related_to_candidates": sum(1 for r in results
                                     if r.get("relevance") == "candidate"),
        "non_candidate": sum(1 for r in results
                             if r.get("relevance") == "non_candidate"),
        "macro": sum(1 for r in results if r.get("relevance") == "macro"),
        "distinct_sec_codes": sorted({r["sec_code"] for r in results if r.get("sec_code")}),
    }
    return summary


# ---------------------------------------------------------------- 7.3
def audit_candidate_selection(candidates_path, market_list_path=None,
                              research_path=None):
    """7.3 候选股票审计。

    检查：ST/退市、权限限制、财务风险、短期涨幅过度依赖、交易成本。

    关键：fundamentals 与 fee 存在于 research.json（研究层），
    不在 stock_candidates.json（初筛层）。两者需合并读取，
    否则会误报"缺少财务数据"。
    """
    cands = load_json(candidates_path)
    candidates = cands.get("candidates", [])

    # 建立 research 层候选索引（含 fundamentals / fee）
    research_by_code = {}
    if research_path and os.path.exists(research_path):
        r = load_json(research_path)
        for rc in r.get("candidates", []):
            if rc.get("code"):
                research_by_code[rc["code"]] = rc

    results = []
    for c in candidates:
        code = c.get("code")
        rc = research_by_code.get(code, {})
        result = {
            "code": code,
            "name": c.get("name"),
            "price": c.get("price"),
            "score": c.get("score"),
            "checks": {},
            "issues": [],
            "verdict": "pass",
        }

        # ST/退市风险
        name = c.get("name", "") or ""
        if name.upper().startswith(("ST", "*ST")) or "退" in name:
            result["checks"]["st_risk"] = "fail"
            _escalate(result, "fail", "存在ST/退市风险")
        else:
            result["checks"]["st_risk"] = "pass"

        # 权限限制
        if str(code).startswith("688"):
            result["checks"]["star_market"] = "warn"
            _escalate(result, "warn", "科创板需50万资产+2年经验权限")
        elif str(code).startswith(("8", "9", "4")):
            result["checks"]["bj_market"] = "warn"
            _escalate(result, "warn", "北交所需单独开通权限")
        else:
            result["checks"]["market_access"] = "pass"

        # 交易成本（fee 来自 research 层）
        fee = rc.get("fee") or c.get("fee") or {}
        if fee:
            breakeven = fee.get("breakeven_pct")
            result["fee_source"] = "research.json" if rc.get("fee") else "candidates"
            if breakeven is None:
                result["checks"]["trade_cost"] = "warn"
                _escalate(result, "warn", "交易成本信息不完整（缺 breakeven_pct）")
            elif breakeven > 2.0:
                result["checks"]["trade_cost"] = "warn"
                _escalate(result, "warn", f"交易成本过高，回本需涨 {breakeven}%")
            else:
                result["checks"]["trade_cost"] = "pass"
                result["breakeven_pct"] = breakeven
        else:
            result["checks"]["trade_cost"] = "warn"
            _escalate(result, "warn", "缺少交易成本信息")

        # 短期涨幅过度依赖
        trend = c.get("trend") or rc.get("trend") or {}
        chg_10d = trend.get("chg_10d_pct") if trend else None
        result["chg_10d_pct"] = chg_10d
        if chg_10d is not None and abs(chg_10d) > 10:
            result["checks"]["short_term_trend"] = "warn"
            _escalate(result, "warn",
                      f"短期涨幅过大（{chg_10d}%），可能过度依赖短期涨幅")
        else:
            result["checks"]["short_term_trend"] = "pass"

        # 财务质量（fundamentals 来自 research 层）
        fund = rc.get("fundamentals") or c.get("fundamentals") or {}
        if fund:
            result["fundamentals_source"] = "research.json" if rc.get("fundamentals") else "candidates"
            profit_yoy = fund.get("profit_yoy")
            roe = fund.get("roe")
            ocf = fund.get("ocf_per_share")
            result["fundamentals_brief"] = {
                "profit_yoy": profit_yoy, "roe": roe, "ocf_per_share": ocf,
                "disclosure_date": fund.get("disclosure_date"),
            }
            if profit_yoy is None:
                result["checks"]["financial_risk"] = "warn"
                _escalate(result, "warn", "净利润同比缺失")
            elif profit_yoy < -20:
                result["checks"]["financial_risk"] = "warn"
                _escalate(result, "warn", f"净利润同比 {profit_yoy}%，大幅下滑")
            else:
                result["checks"]["financial_risk"] = "pass"

            if roe is None:
                result["checks"]["roe"] = "warn"
                _escalate(result, "warn", "ROE 缺失")
            elif roe < 5:
                result["checks"]["roe"] = "warn"
                _escalate(result, "warn", f"ROE {roe}%，偏低")
            else:
                result["checks"]["roe"] = "pass"

            if ocf is None:
                result["checks"]["ocf"] = "warn"
                _escalate(result, "warn", "每股经营现金流缺失")
            elif ocf < 0:
                result["checks"]["ocf"] = "warn"
                _escalate(result, "warn", f"每股经营现金流 {ocf}，为负")
            else:
                result["checks"]["ocf"] = "pass"
        else:
            result["checks"]["financial_data"] = "warn"
            _escalate(result, "warn", "缺少财务数据")

        results.append(result)

    summary = _module_summary("7.3 候选股票审计", results)
    summary["fundamentals_coverage"] = {
        "candidates": len(results),
        "with_fundamentals": sum(1 for r in results if r.get("fundamentals_source")),
        "with_fee": sum(1 for r in results if r.get("fee_source")),
    }
    return summary


# ---------------------------------------------------------------- 7.4
def audit_adversarial_review(risk_review_path):
    """7.4 独立反方审查审计：汇总检查项与判定。"""
    adv = load_json(risk_review_path)
    checks = adv.get("checks", [])
    summary = adv.get("summary", {})

    result = {
        "module": "7.4 独立反方审查",
        "total": len(checks),
        "total_checks": len(checks),
        "passed": sum(1 for c in checks if c.get("verdict") == "pass"),
        "informed": sum(1 for c in checks if c.get("verdict") == "info"),
        "warned": sum(1 for c in checks if c.get("verdict") == "warn"),
        "failed": sum(1 for c in checks if c.get("verdict") == "fail"),
        "source_summary": summary,
        "details": [],
    }
    for c in checks:
        result["details"].append({
            "check": c.get("check"),
            "method": c.get("method"),
            "result": c.get("result"),
            "verdict": c.get("verdict"),
            "evidence_count": len(c.get("evidence", [])),
        })
    return result


# ---------------------------------------------------------------- 汇总
def run_all_audits(reports_dir=REPORTS):
    """运行所有审计模块。使用绝对路径，不依赖当前工作目录。"""
    def rp(name):
        return os.path.join(reports_dir, name)

    results = {"audit_at": iso(), "reports_dir": reports_dir, "modules": []}

    m1 = audit_market_quote(rp("market_snapshot.json"), rp("quality_report.json"))
    results["modules"].append(m1)

    candidates = load_json(rp("stock_candidates.json"))
    candidate_codes = {c.get("code") for c in candidates.get("candidates", [])}
    m2 = audit_news_reliability(rp("news_events.json"), candidate_codes)
    results["modules"].append(m2)

    m3 = audit_candidate_selection(rp("stock_candidates.json"),
                                   research_path=rp("research.json"))
    results["modules"].append(m3)

    m4 = audit_adversarial_review(rp("risk_review.json"))
    results["modules"].append(m4)

    total_checks = total_passed = total_informed = total_warned = total_failed = 0
    for m in results["modules"]:
        total_checks += m.get("total", len(m.get("details", [])))
        total_passed += m.get("passed", 0)
        total_informed += m.get("informed", 0)
        total_warned += m.get("warned", 0)
        total_failed += m.get("failed", 0)

    results["summary"] = {
        "total_checks": total_checks,
        "passed": total_passed,
        "informed": total_informed,
        "warned": total_warned,
        "failed": total_failed,
        "overall": ("fail" if total_failed else ("warn" if total_warned else "pass")),
        "note": "fail 级问题必须修复或明确降级说明；warn 级需在报告中披露。",
    }
    return results


if __name__ == "__main__":
    results = run_all_audits()
    for m in results["modules"]:
        print(f"{m['module']}: total={m.get('total')} pass={m.get('passed')} "
              f"info={m.get('informed', 0)} warn={m.get('warned')} fail={m.get('failed')}")
    s = results["summary"]
    print(f"\n总检查: {s['total_checks']}, 通过: {s['passed']}, "
          f"提示: {s['informed']}, 警告: {s['warned']}, 失败: {s['failed']}")
    print("总体判定:", s["overall"])
    save_json(results, os.path.join(REPORTS, "research_audit.json"))
    print(f"\n审计报告已保存至 {os.path.join(REPORTS, 'research_audit.json')}")
