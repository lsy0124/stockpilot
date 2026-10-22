"""GPT-6 研究交接包扩展（第四阶段升级）。

任务书第九节要求：
- 固定 10 个文件：decision_report.md / market_snapshot.json / news_events.json /
  stock_candidates.json / research.json / risk_review.json / quality_report.json /
  portfolio.json / prediction_history.json / manifest.json
- manifest：唯一交接ID、数据版本、生成时间、来源信息、数据时效、完整性校验、账户状态、历史决策关联
- 额外生成便于上传 ChatGPT 的 ZIP（只含必要报告与非敏感证据，不含密码/Key/Cookie）

用法：
    python -m stockpilot.handoff_v4
"""
import datetime as dt
import hashlib
import json
import os
import zipfile

from stockpilot import REPORTS, audit, iso, load_json, save_json, setup_logger
from stockpilot import ledger as L
from stockpilot import review as review_mod
from stockpilot import timeutil as tu

log = setup_logger("handoff_v4", "handoff_v4.log")

REQUIRED_FILES = [
    "decision_report.md", "market_snapshot.json", "news_events.json",
    "stock_candidates.json", "research.json", "risk_review.json",
    "quality_report.json", "portfolio.json", "prediction_history.json",
    "manifest.json",
]


def _sha256_file(path):
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(65536), b""):
            h.update(chunk)
    return h.hexdigest()[:16]


def build_prediction_history():
    """汇总历史预测（含结算结果），用于交接包"""
    preds = L.get_predictions(limit=50)
    summary = review_mod.review_summary()
    return {
        "generated_at": iso(),
        "summary": summary,
        "predictions": [{
            "id": p["id"], "created_at": p["created_at"], "subject": p["subject"],
            "claim": p["claim"], "horizon": p["horizon"], "code": p.get("code"),
            "base_price": p.get("base_price"),
            "resolved": bool(p["resolved"]), "resolved_at": p.get("resolved_at"),
            "outcome": p.get("outcome"),
        } for p in preds],
        "note": "历史预测不可修改；结算后不可覆盖。预测正确率与交易收益分别统计。",
    }


def build_manifest(handoff_id, files_info, pkg_extra):
    """manifest：交接包元数据与完整性"""
    return {
        "manifest_version": "1.0",
        "handoff_id": handoff_id,
        "generated_at": iso(),
        "generator": "StockPilot V4.0（智谱 GLM5.3）",
        "for": "GPT-6",
        "data_version": {
            "trade_date": pkg_extra.get("trade_date"),
            "market_data_collected_at": pkg_extra.get("collected_at"),
            "news_window": pkg_extra.get("news_window"),
            "fundamentals_report_date": pkg_extra.get("fundamentals_report_date"),
            "freshness": pkg_extra.get("freshness"),
        },
        "files": files_info,
        "integrity": {
            "algorithm": "sha256-16",
            "all_files_present": all(f["present"] for f in files_info),
            "combined_checksum": _combined(files_info),
        },
        "account_status": {
            "kind": "simulated_or_real",
            "initial_capital": pkg_extra.get("initial_capital"),
            "cash": pkg_extra.get("cash"),
            "total_assets": pkg_extra.get("total_assets"),
            "trade_count": pkg_extra.get("trade_count"),
        },
        "history_links": {
            "decisions": pkg_extra.get("decision_count", 0),
            "predictions_total": pkg_extra.get("prediction_total", 0),
            "predictions_resolved": pkg_extra.get("prediction_resolved", 0),
            "previous_handoff_id": pkg_extra.get("previous_handoff_id"),
        },
        "sensitivity_note": "ZIP 仅包含研究报告与结构化数据；不含密码、API Key、"
                            "Cookie、令牌或个人无关文件。",
    }


def _combined(files_info):
    parts = [f"{f['name']}:{f.get('sha256', '')}" for f in files_info]
    return hashlib.sha256("|".join(parts).encode("utf-8")).hexdigest()[:16]


def export_handoff_v4(snapshot, market_list, news, candidates_doc, research, adversarial,
                      quality, portfolio, out_dir=REPORTS, make_zip=True,
                      prev_handoff_id=None):
    """导出 10 文件 + ZIP"""
    from stockpilot import handoff as h_mod
    handoff_id = f"PKG-{tu.now().strftime('%Y%m%d-%H%M%S')}"

    # 1. 基础 9 个文件（含 decision_report.md 与各 JSON）
    paths = {}
    paths["decision_report.md"] = None  # 由 report_mod 生成（下面覆盖）
    from stockpilot import report as report_mod
    base_paths = report_mod.export_all(snapshot, market_list, news, candidates_doc,
                                       research, adversarial, portfolio, quality)
    for k, v in base_paths.items():
        name = os.path.basename(v)
        paths[name] = v

    # 2. prediction_history.json
    ph = build_prediction_history()
    paths["prediction_history.json"] = save_json(ph, os.path.join(out_dir, "prediction_history.json"))

    # 3. 数据质量报告确保导出
    q_path = os.path.join(out_dir, "quality_report.json")
    if not os.path.exists(q_path):
        save_json(quality, q_path)
    paths["quality_report.json"] = q_path

    # 4. manifest（先写入再校验：manifest 自身的 sha256 在写入后回填）
    decisions = L.get_decisions(limit=100)
    first_idx_time = (snapshot.get("indices") or [{}])[0].get("time")
    files_info = []
    for name in REQUIRED_FILES:
        p = os.path.join(out_dir, name)
        exists = os.path.exists(p)
        files_info.append({
            "name": name, "present": exists,
            "size_bytes": os.path.getsize(p) if exists else 0,
            "sha256": _sha256_file(p) if exists else None,
            "role": FILE_ROLES.get(name, ""),
        })
    manifest = build_manifest(handoff_id, files_info, {
        "trade_date": snapshot.get("trade_date"),
        "collected_at": snapshot.get("collected_at"),
        "news_window": news.get("window"),
        "fundamentals_report_date": (research.get("data_as_of") or {}).get(
            "fundamentals_meta", {}).get("report_date"),
        "freshness": {"latest_index_quote_time": first_idx_time,
                      "trade_date_label": tu.trading_day_label(snapshot.get("trade_date"))},
        "initial_capital": portfolio.get("initial_capital"),
        "cash": portfolio.get("cash"),
        "total_assets": portfolio.get("total_assets"),
        "trade_count": portfolio.get("trade_count"),
        "decision_count": len(decisions),
        "prediction_total": ph["summary"].get("resolved_total", 0) + ph["summary"].get("pending_total", 0),
        "prediction_resolved": ph["summary"].get("resolved_total", 0),
        "previous_handoff_id": prev_handoff_id,
    })
    manifest_path = save_json(manifest, os.path.join(out_dir, "manifest.json"))
    paths["manifest.json"] = manifest_path
    # 回填 manifest 自身哈希并重写（保证 all_files_present=True 可验证）
    mf = load_json(manifest_path)
    for f in mf["files"]:
        if f["name"] == "manifest.json":
            f["present"] = True
            f["size_bytes"] = os.path.getsize(manifest_path)
            f["sha256"] = _sha256_file(manifest_path)
    mf["integrity"]["all_files_present"] = all(f["present"] for f in mf["files"])
    mf["integrity"]["combined_checksum"] = _combined(mf["files"])
    save_json(mf, manifest_path)
    manifest = mf

    # 5. ZIP（只含 10 个必要文件）
    zip_path = None
    if make_zip:
        zip_path = os.path.join(out_dir, f"{handoff_id}.zip")
        with zipfile.ZipFile(zip_path, "w", zipfile.ZIP_DEFLATED) as zf:
            for name in REQUIRED_FILES:
                p = os.path.join(out_dir, name)
                if os.path.exists(p):
                    zf.write(p, arcname=f"{handoff_id}/{name}")
            # 附加：数据源健康报告（非敏感证据）
            hp = os.path.join(os.path.dirname(out_dir), "data", "source_health_report.json")
            if os.path.exists(hp):
                zf.write(hp, arcname=f"{handoff_id}/evidence_source_health.json")
        audit("handoff_zip_created", handoff_id=handoff_id, path=zip_path,
              size=os.path.getsize(zip_path))

    # 6. 交接包本体 PKG-*.json（含 12 章节内容，便于独立校验；v4 新增）
    from stockpilot import handoff as h_mod
    pkg_body = h_mod.build_handoff(
        snapshot, market_list, news, candidates_doc,
        {**research, "candidates": research.get("candidates", [])},
        adversarial, quality, portfolio,
        prev_handoff_path=h_mod.latest_handoff_path())
    pkg_body["handoff_id"] = handoff_id          # 与 manifest/zip 使用同一 ID
    pkg_body["v4_manifest_checksum"] = manifest["integrity"]["combined_checksum"]
    pkg_path = save_json(pkg_body, os.path.join(out_dir, f"{handoff_id}.json"))
    paths[f"{handoff_id}.json"] = pkg_path

    # 7. latest 指针
    save_json({"latest_handoff_id": handoff_id, "updated_at": iso(),
               "zip": zip_path, "package": pkg_path,
               "files": {k: v for k, v in paths.items() if v}},
              os.path.join(out_dir, "handoff_latest.json"))

    audit("handoff_v4_exported", handoff_id=handoff_id,
          files_present=sum(1 for f in files_info if f["present"]),
          complete=manifest["integrity"]["all_files_present"])
    return {"handoff_id": handoff_id, "paths": paths, "manifest": manifest,
            "zip": zip_path}


FILE_ROLES = {
    "decision_report.md": "总研究报告（Markdown，供 GPT-6 阅读）",
    "market_snapshot.json": "市场快照（指数/板块/涨跌停，含来源与时间）",
    "news_events.json": "新闻与公告事件（含原始链接/公开时间/可靠性）",
    "stock_candidates.json": "选股候选与筛选规则（含拒绝原因分布）",
    "research.json": "智谱独立研究（证据四分类）",
    "risk_review.json": "反方审查（9 项独立检查）",
    "quality_report.json": "数据质量报告（交叉校验/时效/异常）",
    "portfolio.json": "账户状态（资金/持仓/盈亏）",
    "prediction_history.json": "历史预测与复盘（不可篡改）",
    "manifest.json": "交接包元数据与完整性校验",
}


if __name__ == "__main__":
    snap = load_json(REPORTS + "/market_snapshot.json")
    ml = load_json(REPORTS + "/market_list.json")
    news = load_json(REPORTS + "/news_events.json")
    cands = load_json(REPORTS + "/stock_candidates.json")
    research = load_json(REPORTS + "/research.json")
    adv = load_json(REPORTS + "/risk_review.json")
    qual = load_json(REPORTS + "/quality_report.json")
    pf = L.portfolio()
    res = export_handoff_v4(snap, ml, news, cands, research, adv, qual, pf)
    print("handoff_id:", res["handoff_id"])
    print("files present:", sum(1 for f in res["manifest"]["files"] if f["present"]), "/ 10")
    print("combined checksum:", res["manifest"]["integrity"]["combined_checksum"])
    print("zip:", res["zip"], os.path.getsize(res["zip"]) if res["zip"] else "-", "bytes")
