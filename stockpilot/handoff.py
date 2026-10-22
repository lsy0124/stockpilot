"""GPT-6 标准化研究交接包（第三阶段新增）。

对应任务书第十三章：不依赖 OpenAI API 的标准化交接格式。
每次交接包含 12 项内容，MD + JSON 双格式，
带唯一 ID、生成时间、数据版本与完整性校验（SHA-256）。

产出：
- handoff_<ID>.md / handoff_<ID>.json
- handoff_latest.md / handoff_latest.json（指针）
"""
import datetime as dt
import hashlib
import json
import os

from stockpilot import REPORTS, VAULT, audit, iso, load_json, save_json, setup_logger
from stockpilot import ledger as L
from stockpilot import timeutil as tu
from stockpilot import report as report_mod
from stockpilot import review as review_mod

log = setup_logger("handoff", "handoff.log")

REQUIRED_SECTIONS = [
    "market_overview", "market_data_time", "major_news", "candidates_with_reasons",
    "glm_independent_research", "adversarial_review", "account_status",
    "historical_decisions", "prediction_accuracy", "data_quality_warnings",
    "active_rules", "changes_from_previous",
]


def _sha256(obj):
    """对 JSON 对象计算规范化 SHA-256（用于完整性校验）"""
    return hashlib.sha256(json.dumps(obj, ensure_ascii=False, sort_keys=True,
                                     default=str).encode("utf-8")).hexdigest()[:16]


def build_handoff(snapshot, market_list, news, candidates_doc, research, adversarial,
                  quality, portfolio, rules=None, prev_handoff_path=None):
    """组装交接包。prev_handoff_path 用于生成本次与上次的变化摘要。"""
    handoff_id = f"PKG-{tu.now().strftime('%Y%m%d-%H%M%S')}"
    prev = None
    if prev_handoff_path and os.path.exists(prev_handoff_path):
        try:
            prev = load_json(prev_handoff_path)
        except Exception:
            prev = None

    # 与上次的变化
    changes = {"previous_id": prev.get("handoff_id") if prev else None,
               "notes": []}
    if prev:
        prev_codes = [c["code"] for c in prev.get("candidates_with_reasons", [])]
        cur_codes = [c["code"] for c in candidates_doc.get("candidates", [])]
        added = [c for c in cur_codes if c not in prev_codes]
        removed = [c for c in prev_codes if c not in cur_codes]
        if added or removed:
            changes["notes"].append(f"候选变化：新增 {added}，移除 {removed}")
        else:
            changes["notes"].append("候选名单与上次一致")
        prev_q = prev.get("data_quality_warnings", {})
        cur_q = {"overall": quality.get("overall"),
                 "issues": snapshot.get("data_issues", [])}
        if prev_q.get("overall") != cur_q["overall"]:
            changes["notes"].append(f"数据质量判定 {prev_q.get('overall')} → {cur_q['overall']}")
    else:
        changes["notes"].append("无上次交接记录（首次生成）")

    # 历史决策（账本）
    decisions = L.get_decisions(limit=10)
    pred_summary = review_mod.review_summary()
    predictions = L.get_predictions(limit=10)

    pkg = {
        "handoff_id": handoff_id,
        "generated_at": iso(),
        "generator": "StockPilot（输入研究来源以记录为准）",
        "for": "GPT-6",
        "data_version": {
            "trade_date": snapshot.get("trade_date"),
            "snapshot_collected_at": snapshot.get("collected_at"),
            "latest_quote_time": (snapshot.get("indices") or [{}])[0].get("time"),
            "data_freshness": tu.freshness(
                (snapshot.get("indices") or [{}])[0].get("time")
                and tu.parse_iso((snapshot["indices"][0].get("time"))),
                trade_date=snapshot.get("trade_date")),
        },
        # 1. 最新市场概况
        "market_overview": {
            "indices": snapshot.get("indices", []),
            "breadth": market_list.get("breadth", {}),
            "limit_up_count": snapshot.get("limit_up", {}).get("count"),
            "limit_down_count": snapshot.get("limit_down", {}).get("count"),
            "trade_date_label": tu.trading_day_label(snapshot.get("trade_date")),
            "glm_environment": research.get("market_environment", []),
        },
        # 2. 行情数据时间（显式）
        "market_data_time": {
            "trade_date": snapshot.get("trade_date"),
            "latest_index_quote_time": (snapshot.get("indices") or [{}])[0].get("time"),
            "quote_time_note": (snapshot.get("indices") or [{}])[0].get("time_note"),
            "collected_at": snapshot.get("collected_at"),
            "staleness_note": tu.freshness(
                tu.parse_iso((snapshot.get("indices") or [{}])[0].get("time") or ""),
                trade_date=snapshot.get("trade_date")).get("reason"),
        },
        # 3. 重大新闻及来源
        "major_news": {
            "total": len(news.get("events", [])),
            "official": sum(1 for e in news.get("events", []) if e.get("source_type") == "official"),
            "media": sum(1 for e in news.get("events", []) if e.get("source_type") == "media"),
            "state_media": sum(1 for e in news.get("events", []) if e.get("source_type") == "state_media"),
            "events": [{
                "title": e.get("title"), "published_at": e.get("published_at"),
                "discovered_at": e.get("discovered_at"), "source": e.get("source"),
                "source_type": e.get("source_type"), "url": e.get("source_url"),
                "sec_code": e.get("sec_code"), "reliability": e.get("reliability"),
            } for e in news.get("events", [])[:40]],
        },
        # 4. 股票候选与筛选理由
        "candidates_with_reasons": [{
            "code": c["code"], "name": c.get("name"), "price": c.get("price"),
            "hand_value": c.get("hand_value"), "amount": c.get("amount"),
            "score": c.get("score"), "score_reasons": c.get("score_reasons", []),
            "trend": c.get("trend"),
            "bull_evidence": c.get("bull_evidence", []),
            "bear_evidence": c.get("bear_evidence", []),
            "fee": c.get("fee"),
        } for c in research.get("candidates", [])],
        # 5. 智谱独立研究
        "glm_independent_research": {
            "author": research.get("author"),
            "generated_at": research.get("generated_at"),
            "market_environment": research.get("market_environment", []),
            "strongest_boards": research.get("strongest_boards", []),
            "weakest_boards": research.get("weakest_boards", []),
            "risks": research.get("risks", []),
            "data_gaps": research.get("data_gaps", []),
            "note": "本结论形成时未知 GPT-6 的判断，事后不得修改以迎合 GPT-6",
        },
        # 6. 反方审查
        "adversarial_review": adversarial,
        # 7. 当前账户资金和持仓
        "account_status": {
            "initial_capital": portfolio.get("initial_capital"),
            "cash": portfolio.get("cash"),
            "positions": portfolio.get("positions", []),
            "positions_value": portfolio.get("positions_value"),
            "total_assets": portfolio.get("total_assets"),
            "net_return_pct": portfolio.get("net_return_pct"),
            "trade_count": portfolio.get("trade_count"),
            "fee_model_source": L.fee_model()["source"],
        },
        # 8. 历史相关决策
        "historical_decisions": decisions,
        # 9. 历史预测准确性
        "prediction_accuracy": pred_summary,
        "recent_predictions": [{
            "id": p["id"], "created_at": p["created_at"], "subject": p["subject"],
            "horizon": p["horizon"], "resolved": p["resolved"], "outcome": p["outcome"],
        } for p in predictions],
        # 10. 数据质量警告
        "data_quality_warnings": {
            "overall": quality.get("overall"),
            "snapshot_issues": snapshot.get("data_issues", []),
            "market_list_issues": market_list.get("issues", []),
            "list_quality": quality.get("checks", {}).get("market_list", {}).get("issues", []),
            "known_source_limits": [
                "东财 push2his 历史K线：第一阶段起持续 RST，不可用",
                "东财 push2 clist 全市场列表：第二阶段起持续 502，已切换新浪源",
                "腾讯 fqkline 批量连续请求触发 WAF 501，趋势计算已切换新浪日线（不复权）",
                "akshare stock_news_em：pyarrow 正则兼容 bug，改用 curl 直连",
            ],
        },
        # 11. 当前有效投资规则
        "active_rules": rules or report_mod.default_rules(),
        # 12. 与上次的变化
        "changes_from_previous": changes,
    }
    pkg["integrity"] = {
        "checksum": _sha256(pkg),
        "algorithm": "sha256-16",
        "required_sections": REQUIRED_SECTIONS,
        "sections_present": [s for s in REQUIRED_SECTIONS if s in pkg],
    }
    pkg["integrity"]["complete"] = (pkg["integrity"]["sections_present"] == REQUIRED_SECTIONS)
    return pkg


def render_markdown(pkg):
    """把交接包渲染为 Markdown（总报告 + 指向 JSON 的详细证据）"""
    md = []
    md.append(f"# StockPilot GPT-6 研究交接包 {pkg['handoff_id']}")
    md.append("")
    md.append(f"- 生成时间：{pkg['generated_at']}")
    md.append(f"- 数据交易日：{pkg['data_version']['trade_date']}（{pkg['market_overview']['trade_date_label']}）")
    md.append(f"- 最新行情时间：{pkg['market_data_time']['latest_index_quote_time']}")
    md.append(f"- 时效说明：{pkg['market_data_time']['staleness_note']}")
    md.append(f"- 完整性：{'通过' if pkg['integrity']['complete'] else '不完整'} "
              f"（校验和 {pkg['integrity']['checksum']}）")
    md.append("")
    md.append("## 一、市场概况")
    md.append("")
    for i in pkg["market_overview"]["indices"]:
        md.append(f"- {i.get('name')}：{i.get('close')}（{i.get('pct_chg')}%），"
                  f"行情时间 {i.get('time')}")
    b = pkg["market_overview"]["breadth"]
    if b:
        md.append(f"- 市场宽度：{b.get('total')} 只中 {b.get('up')} 涨 / {b.get('down')} 跌")
    md.append(f"- 涨停 {pkg['market_overview']['limit_up_count']} 只、"
              f"跌停 {pkg['market_overview']['limit_down_count']} 只")
    md.append("")
    md.append("## 二、重大新闻（含来源）")
    md.append("")
    md.append(f"共 {pkg['major_news']['total']} 条：官方公告 {pkg['major_news']['official']}、"
              f"官方媒体 {pkg['major_news']['state_media']}、媒体 {pkg['major_news']['media']}。")
    md.append("")
    md.append("| 发布时间 | 类型 | 标题 | 来源 |")
    md.append("|---|---|---|---|")
    for e in pkg["major_news"]["events"][:15]:
        md.append(f"| {e['published_at']} | {e['source_type']} "
                  f"| {(e['title'] or '')[:40]} | {e['source']} |")
    md.append("")
    md.append("## 三、股票候选与筛选理由")
    md.append("")
    md.append("| 代码 | 名称 | 现价 | 一手 | 规则分 |")
    md.append("|---|---|---|---|---|")
    for c in pkg["candidates_with_reasons"]:
        md.append(f"| {c['code']} | {c.get('name')} | {c.get('price')} "
                  f"| {c.get('hand_value')} | {c.get('score')} |")
    md.append("")
    for c in pkg["candidates_with_reasons"]:
        md.append(f"### {c['code']} {c.get('name')}")
        md.append(f"- 看多：{'；'.join(c['bull_evidence']) or '无'}")
        md.append(f"- 看空：{'；'.join(c['bear_evidence']) or '无'}")
        fee = c.get("fee") or {}
        md.append(f"- 成本：双边 {fee.get('round_trip_total')} 元"
                  f"（回本需涨 {fee.get('breakeven_pct')}%）")
        md.append("")
    md.append("## 四、智谱独立研究结论")
    md.append("")
    for e in pkg["glm_independent_research"]["market_environment"]:
        md.append(f"- {e}")
    md.append("")
    md.append(f"最强行业：{'、'.join(b['name'] + ' ' + str(b['pct_chg']) + '%' for b in pkg['glm_independent_research']['strongest_boards'][:4])}")
    md.append("")
    md.append("## 五、反方审查")
    md.append("")
    for c in pkg["adversarial_review"].get("checks", []):
        md.append(f"- **{c['check']}**（{c['verdict']}）：{c['result']}")
    md.append("")
    md.append(f"总结：{pkg['adversarial_review'].get('summary', {}).get('conclusion')}")
    md.append("")
    md.append("## 六、账户状态")
    md.append("")
    acc = pkg["account_status"]
    md.append(f"- 初始资金 {acc['initial_capital']} 元，现金 {acc['cash']} 元，"
              f"总资产 {acc['total_assets']} 元（净收益率 {acc['net_return_pct']}%）")
    md.append(f"- 持仓 {len(acc['positions'])} 只，已确认成交 {acc['trade_count']} 笔")
    md.append(f"- 费率来源：{acc['fee_model_source']}（交易前必须核实）")
    md.append("")
    md.append("## 七、历史决策与预测准确性")
    md.append("")
    pa = pkg["prediction_accuracy"]
    md.append(f"- 已结算预测 {pa.get('resolved_total')} 条，其中自动评分 {pa.get('auto_scored')} 条，"
              f"方向正确 {pa.get('direction_correct')} 条"
              f"（准确率 {pa.get('accuracy_pct')}%）")
    md.append(f"- 未到期预测 {pa.get('pending_total')} 条（不提前结算）")
    md.append(f"- 历史 GPT-6 决策记录 {len(pkg['historical_decisions'])} 条")
    md.append("")
    md.append("## 八、数据质量警告")
    md.append("")
    md.append(f"总判定：**{pkg['data_quality_warnings']['overall']}**")
    for i in pkg["data_quality_warnings"]["snapshot_issues"]:
        md.append(f"- 快照问题：{i}")
    for i in pkg["data_quality_warnings"]["known_source_limits"]:
        md.append(f"- 已知源限制：{i}")
    md.append("")
    md.append("## 九、当前有效投资规则")
    md.append("")
    for k, v in pkg["active_rules"].items():
        md.append(f"- **{k}**：{v}")
    md.append("")
    md.append("## 十、与上次交接的变化")
    md.append("")
    for n in pkg["changes_from_previous"]["notes"]:
        md.append(f"- {n}")
    md.append("")
    md.append("---")
    md.append("")
    md.append("详细证据（含全部原始字段）见同目录 JSON 文件。")
    return "\n".join(md)


def export_handoff(pkg, out_dir=REPORTS):
    """导出 MD + JSON"""
    base = os.path.join(out_dir, pkg["handoff_id"])
    md_path = base + ".md"
    json_path = base + ".json"
    save_json(pkg, json_path)
    with open(md_path, "w", encoding="utf-8") as f:
        f.write(render_markdown(pkg))
    # latest 指针
    save_json({"latest_handoff_id": pkg["handoff_id"], "updated_at": iso(),
               "files": {"md": md_path, "json": json_path}},
              os.path.join(out_dir, "handoff_latest.json"))
    with open(os.path.join(out_dir, "handoff_latest.md"), "w", encoding="utf-8") as f:
        f.write(open(md_path, encoding="utf-8").read())
    audit("handoff_exported", handoff_id=pkg["handoff_id"], md=md_path, json=json_path,
          complete=pkg["integrity"]["complete"])
    return {"md": md_path, "json": json_path}


def latest_handoff_path(out_dir=REPORTS):
    """上一次交接包的 JSON 路径（用于变化对比）"""
    p = os.path.join(out_dir, "handoff_latest.json")
    if os.path.exists(p):
        try:
            return load_json(p)["files"]["json"]
        except Exception:
            return None
    return None


if __name__ == "__main__":
    snap = load_json(REPORTS + "/market_snapshot.json")
    ml = load_json(REPORTS + "/market_list.json")
    news = load_json(REPORTS + "/news_events.json")
    cands = load_json(REPORTS + "/stock_candidates.json")
    research = load_json(REPORTS + "/research.json")
    adv = load_json(REPORTS + "/risk_review.json")
    qual = load_json(REPORTS + "/quality_report.json")
    pf = L.portfolio()
    pkg = build_handoff(snap, ml, news, cands, research, adv, qual, pf,
                        prev_handoff_path=latest_handoff_path())
    paths = export_handoff(pkg)
    print("handoff:", pkg["handoff_id"], "complete:", pkg["integrity"]["complete"])
    print("checksum:", pkg["integrity"]["checksum"])
    print("files:", paths)
