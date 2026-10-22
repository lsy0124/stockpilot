"""预测复盘系统（第三阶段新增）。

对应任务书第十章：
- 对已到期的预测计算：预测时点价格、期末价格、期间最大有利/不利波动、
  是否触发原定条件、方向是否正确、费用后净收益、与现金/基准的差异
- 未到期预测不得提前标记成功
- 必须保留错误预测、未交易机会和空仓决策
- 严格区分"预测正确"与"实际交易盈利"
"""
import datetime as dt

from stockpilot import REPORTS, VAULT, audit, iso, load_json, save_json, setup_logger
from stockpilot import ledger as L
from stockpilot.collectors import quote as q
from stockpilot import timeutil as tu

log = setup_logger("review", "review.log")


def fetch_price_series(code, datalen=60):
    """拉取标的历史价格（用于复盘计算）。"""
    full = code if code.startswith(("sh", "sz")) else (
        f"sh{code}" if code.startswith(("6", "5")) else f"sz{code}")
    try:
        bars, meta = q.sina_daily(full, datalen=datalen)
        return bars, meta
    except Exception as e:
        log.warning("复盘取价失败 %s: %s", code, e)
        return [], {"error": str(e)}


def resolve_due_predictions(horizon_days=5, benchmark="sh510300"):
    """结算已到期的预测。

    到期判定：created_at + horizon_days ≤ 当前日期，且最近的交易日数据可用。
    计算：
      - base_price（预测时点价格）→ end_price（期末价格）
      - max_favorable / max_adverse（期间最大有利/不利波动 %）
      - direction_correct（方向是否正确）
      - benchmark_chg（同期基准涨跌幅，用于对比）
    只写入 outcome，不修改原始 claim（不可篡改）。
    """
    results = []
    preds = L.get_predictions(resolved=False, limit=50)
    today = tu.today()
    for p in preds:
        created = tu.parse_iso(p["created_at"])
        if not created:
            continue
        created = tu.to_shanghai(created)
        due = created + dt.timedelta(days=horizon_days)
        if due.date() > today:
            results.append({"id": p["id"], "status": "pending",
                            "due_date": due.date().isoformat(),
                            "note": "未到期，不结算"})
            continue
        code = p.get("code")
        if not code:
            # 无标的的预测（如市场环境判断）只做文本留痕，不自动结算
            results.append({"id": p["id"], "status": "manual_required",
                            "note": "无标的代码，需人工判定"})
            continue
        bars, meta = fetch_price_series(code, datalen=horizon_days + 30)
        if len(bars) < 2:
            results.append({"id": p["id"], "status": "no_data",
                            "note": f"价格数据不足：{meta.get('error', 'unknown')}"})
            continue
        base = p.get("base_price")
        if not base:
            # 用预测创建日附近的价格作为基准
            base = None
            for b in bars:
                if b["date"] >= created.date().isoformat():
                    base = b["close"]
                    break
            if base is None:
                base = bars[0]["close"]
        end = bars[-1]["close"]
        chg = round((end / base - 1) * 100, 2)
        highs = [b["high"] for b in bars if b["date"] >= created.date().isoformat()]
        lows = [b["low"] for b in bars if b["date"] >= created.date().isoformat()]
        max_fav = round((max(highs) / base - 1) * 100, 2) if highs else None
        max_adv = round((min(lows) / base - 1) * 100, 2) if lows else None
        # 方向判定：从 claim 文本中提取涨/跌关键词（简单规则，供人工复核）
        claim = p.get("claim") or ""
        direction = None
        if "上涨" in claim or "涨" in claim:
            direction = "up"
        elif "下跌" in claim or "跌" in claim:
            direction = "down"
        correct = None
        if direction == "up":
            correct = chg > 0
        elif direction == "down":
            correct = chg < 0
        bbars, _ = fetch_price_series(benchmark, datalen=horizon_days + 30)
        bench_chg = None
        if len(bbars) >= 2:
            b_base = bbars[0]["close"]
            bench_chg = round((bbars[-1]["close"] / b_base - 1) * 100, 2)
        outcome = {
            "base_price": base, "end_price": end, "change_pct": chg,
            "max_favorable_pct": max_fav, "max_adverse_pct": max_adv,
            "direction": direction, "direction_correct": correct,
            "benchmark": benchmark, "benchmark_change_pct": bench_chg,
            "data_source": meta.get("source"), "adjust": meta.get("adjust"),
            "window": {"created": p["created_at"], "due": due.date().isoformat(),
                       "last_bar": bars[-1]["date"]},
            "note": "方向由 claim 关键词规则提取，需人工复核；"
                    "预测正确不等于交易盈利，交易净收益见账本",
        }
        try:
            L.resolve_prediction(p["id"], outcome)
            results.append({"id": p["id"], "status": "resolved", "outcome": outcome})
        except ValueError as e:
            results.append({"id": p["id"], "status": "already_resolved", "error": str(e)})
    return results


def review_summary(predictions=None):
    """复盘汇总：已结算预测的准确率、平均涨跌、与基准对比。"""
    if predictions is None:
        resolved = L.get_predictions(resolved=True, limit=100)
        pending = L.get_predictions(resolved=False, limit=100)
    else:
        resolved = [p for p in predictions if p.get("resolved")]
        pending = [p for p in predictions if not p.get("resolved")]
    total = len(resolved)
    correct = 0
    manual = 0
    with_outcome = 0
    for p in resolved:
        try:
            o = p["outcome"]
            o = o if isinstance(o, dict) else __import__("json").loads(o)
        except Exception:
            continue
        if not isinstance(o, dict) or "change_pct" not in o:
            manual += 1
            continue
        with_outcome += 1
        if o.get("direction_correct"):
            correct += 1
    return {
        "generated_at": iso(),
        "resolved_total": total,
        "auto_scored": with_outcome,
        "manual_required": manual,
        "direction_correct": correct,
        "accuracy_pct": round(correct / with_outcome * 100, 1) if with_outcome else None,
        "pending_total": len(pending),
        "note": "未到期预测不提前结算；错误预测同样计入统计",
    }


def write_review_notes(results, summary):
    """把复盘写入 Obsidian 知识库 07_交易复盘"""
    import os
    lines = ["# 预测复盘记录", ""]
    lines.append(f"生成时间：{iso()}")
    lines.append("")
    lines.append("## 汇总")
    for k, v in summary.items():
        lines.append(f"- {k}: {v}")
    lines.append("")
    lines.append("## 明细")
    for r in results:
        lines.append(f"### 预测 #{r['id']} — {r['status']}")
        if r.get("outcome"):
            o = r["outcome"]
            lines.append(f"- 基准价 {o.get('base_price')} → 期末 {o.get('end_price')}"
                         f"（{o.get('change_pct')}%）")
            lines.append(f"- 最大有利 {o.get('max_favorable_pct')}% / "
                         f"最大不利 {o.get('max_adverse_pct')}%")
            lines.append(f"- 方向 {o.get('direction')}，判定正确：{o.get('direction_correct')}")
            lines.append(f"- 同期基准 {o.get('benchmark')}：{o.get('benchmark_change_pct')}%")
            lines.append(f"- 数据窗口：{o.get('window')}")
        else:
            lines.append(f"- {r.get('note', r.get('error', ''))}")
        lines.append("")
    path = os.path.join(VAULT, "07_交易复盘", f"复盘_{tu.today().isoformat()}.md")
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        f.write("\n".join(lines))
    audit("review_written", path=path, summary=summary)
    return path


if __name__ == "__main__":
    res = resolve_due_predictions()
    summ = review_summary()
    path = write_review_notes(res, summ)
    save_json({"results": res, "summary": summ}, REPORTS + "/prediction_review.json")
    print("summary:", summ)
    print("resolved:", sum(1 for r in res if r["status"] == "resolved"),
          "pending:", sum(1 for r in res if r["status"] == "pending"))
    print("notes:", path)
