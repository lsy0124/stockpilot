"""第五阶段GPT研究交接包（任务书第十节）。

基于第四阶段handoff_v4.py扩展，新增：
- 数据费用配置（第12项）
- 已知限制（第13项）
- 完整性校验（第14项）

产出：
- decision_report.md
- market_snapshot.json
- news_events.json
- stock_candidates.json
- research.json
- risk_review.json
- quality_report.json
- portfolio.json
- prediction_history.json
- fee_model.json          (新增：交易费用配置)
- data_quality.json       (新增：数据质量状态)
- market_env.json         (新增：市场环境摘要)
- manifest.json           (元数据与完整性校验)
- PKG-*.zip               (压缩包，不含敏感信息)
"""
import datetime as dt
import hashlib
import json
import os
import re
import zipfile

from stockpilot import REPORTS, audit, iso, load_json, save_json, setup_logger
from stockpilot import fee_model
from stockpilot import ledger as L
from stockpilot import timeutil as tu

log = setup_logger("handoff_v5", "handoff_v5.log")

REQUIRED_FILES = [
    "decision_report.md",
    "market_snapshot.json",
    "news_events.json",
    "stock_candidates.json",
    "research.json",
    "risk_review.json",
    "quality_report.json",
    "portfolio.json",
    "prediction_history.json",
    "fee_model.json",
    "data_quality.json",
    "market_env.json",
    "manifest.json",
]


def _sha256_file(path):
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(65536), b""):
            h.update(chunk)
    return h.hexdigest()[:16]


def build_manifest(handoff_id, files_info, pkg_extra):
    """构建manifest：交付包元数据与完整性校验。"""
    return {
        "manifest_version": "1.0",
        "handoff_id": handoff_id,
        "generated_at": iso(),
        "generator": "StockPilot V5.0（智谱 GLM5.3）",
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


SEAL_NAME = "MANIFEST-SHA256.txt"


def package_checksum(pkg):
    """重算交接包本体（PKG-*.json）的校验和。

    与 handoff._sha256 的算法保持一致：对"除 integrity 之外的全部字段"
    做规范化 JSON 哈希。任何在 build_handoff 之后追加的字段
    （handoff_id 覆盖、v5_manifest_checksum 等）都会进入哈希，
    因此必须在所有字段就位后调用本函数重算。
    """
    payload = {k: v for k, v in pkg.items() if k != "integrity"}
    return hashlib.sha256(
        json.dumps(payload, ensure_ascii=False, sort_keys=True,
                   default=str).encode("utf-8")).hexdigest()[:16]


def _combined(files_info):
    """组合校验和：覆盖所有"内容文件"，但排除 manifest 自身。

    排除理由（这是本阶段修复的核心）：manifest.json 无法包含它自身的哈希。
    任何"把自身哈希写进自身"的做法要么不收敛，要么写下的都是旧值 ——
    v5 第一版正是如此，沿用了上一阶段的 3236/2f20202b 僵尸块。
    正确做法是：manifest 只声明"内容文件"的完整性，
    manifest 自身的完整性由独立的 MANIFEST-SHA256.txt 封缄。
    """
    parts = [f"{f['name']}:{f.get('sha256', '')}"
             for f in files_info if f.get("name") != "manifest.json"]
    return hashlib.sha256("|".join(parts).encode("utf-8")).hexdigest()[:16]


def write_seal(manifest_path, out_dir=None):
    """写出 manifest.json 的独立封缄文件（内容即 manifest 的 sha256-16 与字节数）。"""
    out_dir = out_dir or os.path.dirname(manifest_path)
    seal_path = os.path.join(out_dir, SEAL_NAME)
    body = (f"file: manifest.json\n"
            f"sha256_16: {_sha256_file(manifest_path)}\n"
            f"size_bytes: {os.path.getsize(manifest_path)}\n"
            f"algorithm: sha256-16\n"
            f"sealed_at: {iso()}\n"
            f"note: 本文件是 manifest.json 的独立封缄；"
            f"manifest 内部不再记录自身哈希（自指不可能自洽）。\n")
    with open(seal_path, "w", encoding="utf-8", newline="\n") as f:
        f.write(body)
    return seal_path


def verify_manifest(manifest_path, out_dir=None, require_seal=True):
    """独立校验 manifest 声明的完整性是否与磁盘真实字节一致。

    这正是 v5 第一版缺失的检查：原实现只断言 all_files_present 这个
    自我声明的布尔值，从不重算哈希，导致"声明存在但磁盘缺失"、
    "哈希陈旧"、以及"自身哈希沿用上一阶段旧值"三类问题全部漏过。

    Returns:
        {"ok": bool, "problems": [...], "manifest": dict, "seal": dict|None}
    """
    out_dir = out_dir or os.path.dirname(manifest_path)
    problems = []
    manifest = load_json(manifest_path)
    if not isinstance(manifest, dict):
        return {"ok": False, "problems": ["manifest 必须为对象"], "manifest": manifest, "seal": None}
    files = manifest.get("files", [])
    # A manifest is untrusted input. Validate every name before reading any file.
    names = set()
    if not isinstance(files, list) or not files:
        problems.append("manifest files 必须为非空列表")
        files = []
    for entry in files:
        name = entry.get("name") if isinstance(entry, dict) else None
        if (not isinstance(name, str) or not name or name in (".", "..")
                or any(char in name for char in ("/", "\\", ":", "\x00"))
                or name.casefold() in names):
            problems.append("manifest 含无效、重复或越界文件名")
            continue
        names.add(name.casefold())
        target = os.path.realpath(os.path.join(out_dir, name))
        root = os.path.realpath(out_dir)
        if os.path.commonpath([root, target]) != root or os.path.islink(os.path.join(out_dir, name)):
            problems.append("manifest 文件不得重定向至包目录之外")
    if problems:
        return {"ok": False, "problems": problems, "manifest": manifest, "seal": None}

    # 1. manifest 不得为自身声明哈希（自指无法自洽）
    self_entries = [f for f in files if f.get("name") == "manifest.json"]
    for f in self_entries:
        if f.get("sha256") or f.get("size_bytes"):
            problems.append(
                "manifest.json 不得记录自身 sha256/size（自指哈希无法自洽，"
                "请改用 MANIFEST-SHA256.txt 封缄）")

    # 2. 内容文件逐个与磁盘比对
    for f in files:
        name = f.get("name")
        if name == "manifest.json":
            continue
        p = os.path.join(out_dir, name)
        if not f.get("present"):
            problems.append(f"{name}: 声明为缺失（present=false）")
            continue
        if not os.path.isfile(p):
            problems.append(f"{name}: 声明存在但磁盘上不存在")
            continue
        actual_size = os.path.getsize(p)
        actual_sha = _sha256_file(p)
        if actual_size != f.get("size_bytes"):
            problems.append(
                f"{name}: size 不符（声明 {f.get('size_bytes')}，实际 {actual_size}）")
        if actual_sha != f.get("sha256"):
            problems.append(
                f"{name}: sha256 不符（声明 {f.get('sha256')}，实际 {actual_sha}）")

    # 3. 汇总标记
    real_all = all(f.get("present") and os.path.isfile(os.path.join(out_dir, f["name"]))
                   for f in files if f.get("name") != "manifest.json")
    declared_all = manifest.get("integrity", {}).get("all_files_present")
    if declared_all != real_all:
        problems.append(
            f"integrity.all_files_present 声明 {declared_all}，实际 {real_all}")

    recomputed = _combined(files)
    declared_comb = manifest.get("integrity", {}).get("combined_checksum")
    if recomputed != declared_comb:
        problems.append(
            f"combined_checksum 无法重算（声明 {declared_comb}，重算 {recomputed}）")

    # 4. 封缄文件校验（manifest 自身完整性）
    seal = None
    if require_seal:
        seal_path = os.path.join(out_dir, SEAL_NAME)
        if os.path.islink(seal_path) or os.path.dirname(os.path.realpath(seal_path)) != os.path.realpath(out_dir):
            problems.append("封缄文件不得重定向至包目录之外")
        elif not os.path.isfile(seal_path):
            problems.append(f"缺少封缄文件 {SEAL_NAME}，manifest 自身完整性不可验证")
        else:
            txt = open(seal_path, encoding="utf-8").read()
            decl_sha = re.search(r"sha256_16:\s*([0-9a-f]{16})", txt)
            decl_size = re.search(r"size_bytes:\s*(\d+)", txt)
            real_sha = _sha256_file(manifest_path)
            real_size = os.path.getsize(manifest_path)
            seal = {"declared_sha256": decl_sha.group(1) if decl_sha else None,
                    "declared_size": int(decl_size.group(1)) if decl_size else None,
                    "actual_sha256": real_sha, "actual_size": real_size}
            if not decl_sha or decl_sha.group(1) != real_sha:
                problems.append(
                    f"manifest.json 与封缄不符（封缄 {seal['declared_sha256']}，"
                    f"实际 {real_sha}）")
            if not decl_size or int(decl_size.group(1)) != real_size:
                problems.append(
                    f"manifest.json 尺寸与封缄不符（封缄 {seal['declared_size']}，"
                    f"实际 {real_size}）")

    return {"ok": not problems, "problems": problems,
            "manifest": manifest, "seal": seal}


def export_handoff_v5(snapshot, market_list, news, candidates_doc, research, adversarial,
                      quality, portfolio, out_dir=REPORTS, make_zip=True,
                      prev_handoff_id=None):
    """导出第五阶段GPT研究交接包（14项内容 + ZIP）。"""
    handoff_id = f"PKG-{tu.now().strftime('%Y%m%d-%H%M%S')}"

    # 基础报告文件（调用第四阶段的report模块）
    from stockpilot import report as report_mod
    base_paths = report_mod.export_all(
        snapshot, market_list, news, candidates_doc,
        research, adversarial, portfolio, quality, out_dir=out_dir
    )
    paths = {}
    for k, v in base_paths.items():
        name = os.path.basename(v)
        paths[name] = v

    # 费用配置文件
    fee_model_path = os.path.join(out_dir, "fee_model.json")
    fee_data = {
        "generated_at": iso(),
        "source": "mixed",
        "note": ("费率分两类：印花税/过户费/经手费/证管费为交易所与税务机关"
                 "公开标准（official）；券商佣金率与最低佣金为假设值"
                 "（assumption），因券商与客户而异，无法从公开渠道确认，"
                 "交易前必须向你的券商核实。详见 docs/费率核实清单.md"),
        "official_rates": {
            "stamp_duty_sell": 0.0005,
            "stamp_duty_note": "2023-08-28 起减半征收，卖出单边；ETF/基金免征",
            "transfer_fee": 0.00001,
            "handling_fee": 0.0000341,
            "supervision_fee": 0.00002,
            "handling_fee_source": "上交所《收费一览表》(2026年1月更新)",
            "verified_at": "2026-10-21",
        },
        "unverified": ["券商佣金率", "最低佣金（是否免五）", "其他券商自定费用"],
        "configs": [
            {
                "config_id": c.config_id,
                "name": c.name,
                "source": c.source,
                "effective_date": c.effective_date,
                "is_etf": c.is_etf,
                "market": c.market,
                "commission_rate_buy": c.commission_rate_buy,
                "commission_rate_sell": c.commission_rate_sell,
                "commission_min_buy": c.commission_min_buy,
                "commission_min_sell": c.commission_min_sell,
                "stamp_duty_rate_sell": c.stamp_duty_rate_sell,
                "transfer_fee_rate_buy": c.transfer_fee_rate_buy,
                "transfer_fee_rate_sell": c.transfer_fee_rate_sell,
                "other_fee_rate": c.other_fee_rate,
                "notes": c.notes,
            } for c in fee_model.fee_manager.list_configs()
        ],
    }
    save_json(fee_data, fee_model_path)
    paths["fee_model.json"] = fee_model_path

    # 数据质量状态文件
    dq_path = os.path.join(out_dir, "data_quality.json")
    dq_data = {
        "generated_at": iso(),
        "overall": quality.get("overall"),
        "checks": quality.get("checks", {}),
        "sources": snapshot.get("sources", {}),
        "data_issues": snapshot.get("data_issues", []),
        "market_list_issues": market_list.get("issues", []),
        "known_limitations": [
            "东财 push2his 历史K线接口不可用（RST）",
            "东财 push2 clist 全市场列表接口不可用（502）",
            "腾讯 fqkline 日线接口触发 WAF 限制（501）",
            "新浪日线为不复权价，除权日存在跳空",
            "央视新闻周末无数据",
            "东财个股新闻为单源，无交叉验证",
        ],
    }
    save_json(dq_data, dq_path)
    paths["data_quality.json"] = dq_path

    # 市场环境文件
    me_path = os.path.join(out_dir, "market_env.json")
    me_data = {
        "generated_at": iso(),
        "trade_date": snapshot.get("trade_date"),
        "indices": snapshot.get("indices", []),
        "boards": snapshot.get("boards", []),
        "breadth": market_list.get("breadth", {}),
        "limit_up_count": snapshot.get("limit_up", {}).get("count"),
        "limit_down_count": snapshot.get("limit_down", {}).get("count"),
        "market_environment": research.get("market_environment", []) if research else [],
        "strongest_boards": research.get("strongest_boards", []) if research else [],
        "weakest_boards": research.get("weakest_boards", []) if research else [],
    }
    save_json(me_data, me_path)
    paths["market_env.json"] = me_path

    # manifest
    # 历史留痕：必须来自账本本身，不得硬编码 0
    decisions = L.get_decisions(limit=100)
    preds = L.get_predictions(limit=1000)
    prediction_total = len(preds)
    prediction_resolved = sum(1 for p in preds if p.get("resolved"))
    # Build every required content file in the requested output directory.
    # A clean export must not depend on stale reports from a previous pipeline.
    paths["quality_report.json"] = save_json(quality, os.path.join(out_dir, "quality_report.json"))
    from stockpilot import review as review_mod
    paths["prediction_history.json"] = save_json({
        "predictions": preds,
        "summary": review_mod.review_summary(predictions=preds),
    }, os.path.join(out_dir, "prediction_history.json"))

    def _collect_files():
        info = []
        for name in REQUIRED_FILES:
            if name == "manifest.json":
                # 自指：manifest 不记录自身哈希，改由 MANIFEST-SHA256.txt 封缄
                info.append({
                    "name": name,
                    "present": True,
                    "size_bytes": None,
                    "sha256": None,
                    "self_reference": False,
                    "role": "交接包元数据与完整性校验（自身完整性见 "
                            + SEAL_NAME + "）",
                })
                continue
            p = os.path.join(out_dir, name)
            exists = os.path.isfile(p)
            info.append({
                "name": name,
                "present": exists,
                "size_bytes": os.path.getsize(p) if exists else 0,
                "sha256": _sha256_file(p) if exists else None,
            })
        return info

    manifest = build_manifest(handoff_id, _collect_files(), {
        "trade_date": snapshot.get("trade_date"),
        "collected_at": snapshot.get("collected_at"),
        "news_window": news.get("window"),
        "fundamentals_report_date": (research.get("data_as_of") or {}).get(
            "fundamentals_meta", {}).get("report_date") if research else None,
        "freshness": {
            "latest_index_quote_time": (snapshot.get("indices") or [{}])[0].get("time"),
            "trade_date_label": tu.trading_day_label(snapshot.get("trade_date")),
        },
        "initial_capital": portfolio.get("initial_capital"),
        "cash": portfolio.get("cash"),
        "total_assets": portfolio.get("total_assets"),
        "trade_count": portfolio.get("trade_count"),
        "decision_count": len(decisions),
        "prediction_total": prediction_total,
        "prediction_resolved": prediction_resolved,
        "previous_handoff_id": prev_handoff_id,
    })
    manifest_path = save_json(manifest, os.path.join(out_dir, "manifest.json"))
    paths["manifest.json"] = manifest_path

    # 封缄：manifest 自身完整性由独立文件记录（正是不收敛问题的正确解法）
    seal_path = write_seal(manifest_path, out_dir)
    paths[SEAL_NAME] = seal_path

    # 交付前硬校验：任何一项不符即拒绝产出（防止再次交付自相矛盾的 manifest）
    verify = verify_manifest(manifest_path, out_dir)
    if not verify["ok"]:
        raise RuntimeError(
            f"交接包完整性自检失败，已中止导出：{verify['problems']}")
    manifest = verify["manifest"]

    # ZIP（同时封入 manifest 与其封缄文件）
    zip_path = None
    if make_zip:
        zip_path = os.path.join(out_dir, f"{handoff_id}.zip")
        with zipfile.ZipFile(zip_path, "w", zipfile.ZIP_DEFLATED) as zf:
            for name in REQUIRED_FILES:
                p = os.path.join(out_dir, name)
                if os.path.exists(p):
                    zf.write(p, arcname=f"{handoff_id}/{name}")
            # manifest 自身的封缄文件
            if os.path.exists(seal_path):
                zf.write(seal_path, arcname=f"{handoff_id}/{SEAL_NAME}")
            # 附加数据源健康报告（非敏感证据）
            hp = os.path.join(os.path.dirname(out_dir), "data", "source_health_report.json")
            if os.path.exists(hp):
                zf.write(hp, arcname=f"{handoff_id}/evidence_source_health.json")
        audit("handoff_v5_zip_created", handoff_id=handoff_id, path=zip_path,
              size=os.path.getsize(zip_path))

    # 交接包本体 PKG-*.json（12 章节内容，便于独立校验）
    # v5 第一版弄丢了这一产出，导致 handoff_latest.json 的 package 指针悬空。
    from stockpilot import handoff as h_mod
    pkg_body = h_mod.build_handoff(
        snapshot, market_list, news, candidates_doc,
        research, adversarial, quality, portfolio,
        prev_handoff_path=h_mod.latest_handoff_path(),
    )
    pkg_body["handoff_id"] = handoff_id   # 与 manifest/zip 使用同一 ID
    pkg_body["v5_manifest_checksum"] = manifest["integrity"]["combined_checksum"]
    # 关键：handoff.build_handoff 在设置 integrity 之前就算好 checksum，
    # 而上面我们又追加了两个字段（handoff_id 覆盖、v5_manifest_checksum），
    # 使原 checksum 与人眼看到的包体不再对应。此处必须重算，
    # 否则任何独立校验者都会得出"校验和不符"（曾导致交付包自检失败）。
    pkg_body["integrity"]["checksum"] = package_checksum(pkg_body)
    pkg_path = save_json(pkg_body, os.path.join(out_dir, f"{handoff_id}.json"))
    paths[f"{handoff_id}.json"] = pkg_path

    # latest 指针（package 必须指向真实存在的本体）
    save_json({"latest_handoff_id": handoff_id, "updated_at": iso(),
               "zip": zip_path, "package": pkg_path,
               "files": {k: v for k, v in paths.items() if v}},
              os.path.join(out_dir, "handoff_latest.json"))

    audit("handoff_v5_exported", handoff_id=handoff_id,
          files_present=sum(1 for f in manifest["files"] if f["present"]),
          complete=manifest["integrity"]["all_files_present"])

    return {
        "handoff_id": handoff_id,
        "paths": paths,
        "manifest": manifest,
        "zip": zip_path,
        "package": pkg_path,
        "verification": verify,
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
    res = export_handoff_v5(snap, ml, news, cands, research, adv, qual, pf)
    n_req = len(REQUIRED_FILES)
    print("handoff_id:", res["handoff_id"])
    print("files present:",
          sum(1 for f in res["manifest"]["files"] if f["present"]), "/", n_req)
    print("combined checksum:", res["manifest"]["integrity"]["combined_checksum"])
    print("self-verification:", "OK" if res["verification"]["ok"] else res["verification"]["problems"])
    print("zip:", res["zip"], os.path.getsize(res["zip"]) if res["zip"] else "-", "bytes")
    print("package:", res["package"], os.path.getsize(res["package"]) if res.get("package") else "-", "bytes")