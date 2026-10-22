"""Obsidian 知识库同步增强（第三阶段重写）。

对应任务书第八、九章：
- 自动归档每日研究
- 自动保存股票候选
- 自动保存智谱看多/看空理由
- 自动保存重大事件（02_重大新闻与公告）
- 自动记录历史预测（05_历史预测）
- 自动生成投资复盘（07_交易复盘）
- 按股票代码/日期/主题检索（search）
- 从当前研究跳转到历史相关记录（交叉链接）
"""
import os
import re
import shutil

from stockpilot import REPORTS, VAULT, audit, iso, load_json, setup_logger
from stockpilot import ledger as L
from stockpilot import timeutil as tu

log = setup_logger("vault_sync", "vault_sync.log")


def _w(path, lines):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        f.write("\n".join(lines))


def sync_daily(trade_date=None):
    """每日归档：报告 + 候选 + 事件 + 决策 + 预测"""
    trade_date = trade_date or tu.today().isoformat()
    out = {}

    # 1. 每日研究报告
    src_md = os.path.join(REPORTS, "decision_report.md")
    if os.path.exists(src_md):
        dst = os.path.join(VAULT, "01_每日市场报告", f"{trade_date}_研究报告.md")
        os.makedirs(os.path.dirname(dst), exist_ok=True)
        shutil.copy2(src_md, dst)
        out["daily_report"] = dst

    # 2. 候选（含正反理由）
    cands_path = os.path.join(REPORTS, "stock_candidates.json")
    if os.path.exists(cands_path):
        c = load_json(cands_path)
        lines = [f"# {trade_date} 选股候选（程序化初筛）", "",
                 f"生成时间：{c.get('screened_at')}，规则分与证据如下。", ""]
        for x in c.get("candidates", []):
            t = x.get("trend") or {}
            lines.append(f"## {x['code']} {x.get('name')}（规则分 {x.get('score')}）")
            lines.append(f"- 现价 {x.get('price')}，一手 {x.get('hand_value')} 元，"
                         f"日成交额 {(x.get('amount') or 0)/1e8:.1f} 亿元")
            if t.get("ok"):
                lines.append(f"- 趋势：收盘 {t.get('close')} / MA20 {t.get('ma20')} / "
                             f"MA60 {t.get('ma60')}；10日 {t.get('chg_10d_pct')}%，"
                             f"60日 {t.get('chg_60d_pct')}%"
                             f"（源：{t.get('source')}，{t.get('adjust')}）")
            lines.append(f"- 评分依据：{'；'.join(x.get('score_reasons', []))}")
            lines.append("")
        dst = os.path.join(VAULT, "04_股票候选", f"{trade_date}_候选.md")
        _w(dst, lines)
        out["candidates"] = dst

    # 3. 重大事件（02）
    news_path = os.path.join(REPORTS, "news_events.json")
    if os.path.exists(news_path):
        n = load_json(news_path)
        lines = [f"# {trade_date} 重大事件留痕", "",
                 f"采集时间：{n.get('collected_at')}，窗口 {n.get('window')}，"
                 f"共 {len(n.get('events', []))} 条（去重前 {n.get('dedup', {}).get('before')}）。", ""]
        for e in n.get("events", [])[:60]:
            lines.append(f"## {e.get('published_at')} | {e.get('title')}")
            lines.append(f"- 类型：{e.get('type')}｜来源：{e.get('source')}"
                         f"（{e.get('source_type')}，可靠性 {e.get('reliability')}）")
            lines.append(f"- 原始链接：{e.get('source_url') or e.get('url')}")
            lines.append(f"- 相关证券：{e.get('sec_code') or '-'} {e.get('sec_name') or ''}")
            lines.append(f"- 首次公开时间：{e.get('published_at')}｜系统发现时间：{e.get('discovered_at')}")
            lines.append(f"- 摘要：{(e.get('summary') or '')[:160]}")
            lines.append("")
        dst = os.path.join(VAULT, "02_重大新闻与公告", f"{trade_date}_事件.md")
        _w(dst, lines)
        out["events"] = dst

    # 4. 决策与预测留痕（05/06）
    try:
        preds = L.get_predictions(limit=20)
        lines = ["# 历史预测留痕（不可修改）", "",
                 "> 原始预测一经记录不得覆盖；修订必须新建记录。", ""]
        for p in preds:
            lines.append(f"## #{p['id']} {p['created_at']} — {p['subject']}")
            lines.append(f"- 内容：{p['claim']}")
            lines.append(f"- 时间范围：{p['horizon']}｜状态："
                         f"{'已结算' if p['resolved'] else '待验证'}")
            if p.get("resolved") and p.get("outcome"):
                lines.append(f"- 结算结果：{p['outcome']}")
            lines.append("")
        _w(os.path.join(VAULT, "05_历史预测", "预测留痕.md"), lines)

        decisions = L.get_decisions(limit=20)
        if decisions:
            dlines = ["# 投资决策记录（GPT-6）", "",
                      "> GPT-6 的正式决定必须经用户确认或可核验交接导入；"
                      "智谱研究意见不自动记为 GPT-6 决定。", ""]
            for d in decisions:
                dlines.append(f"## {d['decision_uid']} {d['decided_at']}")
                dlines.append(f"- 标的：{d['code']} {d.get('name') or ''}｜方向：{d['direction']}")
                dlines.append(f"- 价格区间：{d.get('price_low')} ~ {d.get('price_high')}｜"
                              f"数量：{d.get('shares')}")
                dlines.append(f"- 数据版本：{d.get('data_version')}｜"
                              f"智谱研究引用：{d.get('glm_research_ref')}")
                dlines.append(f"- 看多：{d.get('reason_bull') or '-'}")
                dlines.append(f"- 看空：{d.get('reason_bear') or '-'}")
                dlines.append(f"- 风险条件：{d.get('risk_condition') or '-'}｜"
                              f"退出条件：{d.get('exit_condition') or '-'}")
                dlines.append(f"- 是否执行：{'是' if d.get('executed') == 1 else ('已修订' if d.get('executed') == -1 else '否')}")
                dlines.append("")
            _w(os.path.join(VAULT, "06_交易决策", "决策记录.md"), dlines)
            out["decisions"] = os.path.join(VAULT, "06_交易决策", "决策记录.md")
    except Exception as e:
        log.warning("决策/预测同步失败: %s", e)

    # 5. 公司研究（03）：从候选生成公司研究页（含交叉链接）
    if os.path.exists(cands_path):
        c = load_json(cands_path)
        for x in c.get("candidates", [])[:10]:
            code = x["code"]
            t = x.get("trend") or {}
            lines = [f"# {code} {x.get('name')}", "",
                     f"- 数据日期：{trade_date}", f"- 现价：{x.get('price')}",
                     f"- 一手价格：{x.get('hand_value')} 元（1000元账户"
                     f"{'可交易' if (x.get('hand_value') or 9999) <= 900 else '不可交易'}）", ""]
            if t.get("ok"):
                lines += ["## 趋势", "",
                          f"- 收盘 {t.get('close')}，MA20 {t.get('ma20')}，MA60 {t.get('ma60')}",
                          f"- 10日涨幅 {t.get('chg_10d_pct')}%，60日涨幅 {t.get('chg_60d_pct')}%", ""]
            lines += ["## 相关链接", "",
                      f"- [[{trade_date}_候选]]",
                      f"- [[{trade_date}_研究报告]]",
                      f"- [[{trade_date}_事件]]", ""]
            _w(os.path.join(VAULT, "03_公司研究", f"{code}_{x.get('name')}.md"), lines)
        out["company_notes"] = len(c.get("candidates", [])[:10])

    audit("vault_synced", trade_date=trade_date, outputs=out)
    return out


# ---------------------------------------------------------------- 检索
def search(query, kind=None):
    """按关键词检索知识库（股票代码/日期/主题）。

    返回 [{file, section, snippet, matches}]，按匹配数排序。
    kind 可限定目录名（如 "01_每日市场报告"）。
    """
    results = []
    q = str(query).strip().lower()
    if not q:
        return results
    for root, dirs, files in os.walk(VAULT):
        if kind and os.path.basename(root) != kind:
            continue
        for fn in files:
            if not fn.endswith(".md"):
                continue
            path = os.path.join(root, fn)
            try:
                text = open(path, encoding="utf-8").read()
            except Exception:
                continue
            low = text.lower()
            hits = low.count(q)
            if hits == 0:
                continue
            i = low.find(q)
            snippet = re.sub(r"\s+", " ", text[max(0, i - 60):i + 120])
            results.append({"file": os.path.relpath(path, VAULT), "matches": hits,
                            "snippet": snippet})
    results.sort(key=lambda r: -r["matches"])
    return results


def related_notes(code, limit=10):
    """从某只股票跳转到历史相关记录"""
    return search(code)[:limit]


if __name__ == "__main__":
    out = sync_daily()
    print("synced:", out)
    print("\n检索 600028：")
    for r in search("600028")[:5]:
        print(f"  [{r['matches']}次] {r['file']} — {r['snippet'][:70]}")
