"""GPT-6 研究报告导出模块。

对应任务书第十四章：每次报告必须包含 12 项内容，并同时生成
market_snapshot.json / news_events.json / stock_candidates.json /
risk_review.json / portfolio.json / decision_report.md。
报告必须能在不依赖 OpenAI API 的情况下生成。
"""
import datetime as dt
import os

from stockpilot import REPORTS, VAULT, iso, load_json, save_json, setup_logger, audit
from stockpilot import ledger

log = setup_logger("report", "report.log")



def _ev_text(evs):
    """证据列表转文本：兼容 str 与 {type,text} dict 两种格式（第四阶段证据四分类）"""
    if not evs:
        return ""
    parts = []
    for e in evs:
        if isinstance(e, dict):
            parts.append(f"[{e.get('type','?')}] {e.get('text','')}")
        else:
            parts.append(str(e))
    return "；".join(parts)


def md_escape(s):
    return str(s).replace("|", "\\|").replace("\n", " ")


def build_decision_report(snapshot, market_list, news, candidates, research, adversarial,
                          portfolio, quality, rules):
    """生成 Markdown 研究报告"""
    now = iso()
    td = snapshot.get("trade_date")
    L = []
    L.append(f"# StockPilot GPT-6 研究报告")
    L.append("")
    L.append(f"- 报告生成时间：{now}")
    L.append(f"- 数据截止交易日：{td}（采集于 {snapshot.get('collected_at')}）")
    L.append(f"- 数据质量总判定：**{quality.get('overall')}**")
    L.append("- 本报告由 StockPilot 根据输入数据生成；研究来源以输入记录为准，仅用于研究与模拟。")
    L.append("")

    # 2. 当前有效投资规则
    L.append("## 一、当前有效投资规则")
    L.append("")
    for k, v in rules.items():
        L.append(f"- **{k}**：{v}")
    L.append("")

    # 3. 市场整体行情
    L.append("## 二、市场整体行情")
    L.append("")
    L.append("| 指数 | 收盘 | 涨跌幅 | 行情时间 |")
    L.append("|---|---|---|---|")
    for i in snapshot.get("indices", []):
        L.append(f"| {md_escape(i.get('name'))} | {i.get('close')} | {i.get('pct_chg')}% "
                 f"| {i.get('time')} |")
    L.append("")
    b = market_list.get("breadth", {})
    if b:
        L.append(f"市场宽度：共 {b.get('total')} 只，上涨 {b.get('up')} 只、下跌 {b.get('down')} 只、"
                 f"平盘 {b.get('flat')} 只（涨跌比 "
                 f"{(b.get('up',0)/max(b.get('down',1),1)):.2f}）。")
        L.append("")
    zu = snapshot.get("limit_up", {})
    L.append(f"涨停 {zu.get('count')} 只、跌停 {snapshot.get('limit_down',{}).get('count')} 只"
             f"（{zu.get('date')}）。")
    L.append("")

    # 4. 重大新闻及来源
    L.append("## 三、重大新闻及来源")
    L.append("")
    events = news.get("events", [])
    L.append(f"共采集 {len(events)} 条事件（官方公告 {sum(1 for e in events if e.get('source_type')=='official')} 条、"
             f"媒体 {sum(1 for e in events if e.get('source_type')=='media')} 条、"
             f"官方媒体 {sum(1 for e in events if e.get('source_type')=='state_media')} 条），"
             f"去重前 {news.get('dedup',{}).get('before')} 条。")
    L.append("")
    L.append("| 发布时间 | 类型 | 相关证券 | 标题 | 来源 | 可靠性 |")
    L.append("|---|---|---|---|---|---|")
    for e in events[:25]:
        L.append(f"| {md_escape(e.get('published_at'))} | {e.get('type')} "
                 f"| {md_escape(e.get('sec_name') or e.get('sec_code') or '-')} "
                 f"| {md_escape((e.get('title') or '')[:44])} | {md_escape(e.get('source'))} "
                 f"| {e.get('reliability')} |")
    L.append("")
    L.append("> 注：官方公告为事实；媒体新闻为转述/观点，须区分对待。完整列表见 `news_events.json`。")
    L.append("")

    # 5. 行业强弱
    L.append("## 四、行业强弱")
    L.append("")
    L.append("| 行业 | 涨跌幅 | 上涨家数 | 下跌家数 | 领涨股 |")
    L.append("|---|---|---|---|---|")
    for bd in research.get("strongest_boards", []):
        L.append(f"| {md_escape(bd.get('name'))} | {bd.get('pct_chg')}% | {bd.get('up')} "
                 f"| {bd.get('down')} | {md_escape(bd.get('lead_stock') or '-')} |")
    L.append("")
    if research.get("weakest_boards"):
        L.append("最弱行业：" + "、".join(f"{b['name']}（{b['pct_chg']}%）"
                                          for b in research["weakest_boards"]))
        L.append("")

    # 6/7. 重点候选与正反证据
    L.append("## 五、重点候选股票")
    L.append("")
    L.append("> 候选来自本次输入的规则筛选结果；覆盖范围以本次数据为准，不预设全市场数量。")
    L.append("")
    L.append("| 代码 | 名称 | 现价 | 一手价格 | 日成交额 | 规则分 |")
    L.append("|---|---|---|---|---|---|")
    for c in research.get("candidates", []):
        L.append(f"| {c['code']} | {md_escape(c['name'])} | {c.get('price')} "
                 f"| {c.get('hand_value')} | {(c.get('amount') or 0)/1e8:.1f}亿 "
                 f"| {c.get('score')} |")
    L.append("")
    for c in research.get("candidates", []):
        L.append(f"### {c['code']} {md_escape(c['name'])}")
        t = c.get("trend") or {}
        if t.get("ok"):
            L.append(f"- 趋势：收盘 {t.get('close')}，20日均线 {t.get('ma20')}，"
                     f"60日均线 {t.get('ma60')}；站上MA20={t.get('above_ma20')}，"
                     f"站上MA60={t.get('above_ma60')}；10日涨幅 {t.get('chg_10d_pct')}%，"
                     f"60日涨幅 {t.get('chg_60d_pct')}%")
        L.append("- **看多证据**：" + (_ev_text(c.get("bull_evidence")) or "无"))
        L.append("- **看空证据**：" + (_ev_text(c.get("bear_evidence")) or "无"))
        f = c.get("fee") or {}
        L.append(f"- 1000元可交易性：一手 {c.get('hand_value')} 元，"
                 f"双边成本 {f.get('round_trip_total')} 元"
                 f"（占本金 {f.get('pct_of_capital')}%，回本需涨 {f.get('breakeven_pct')}%）")
        L.append("")

    # 8. 账户状态
    L.append("## 六、当前账户状态")
    L.append("")
    L.append(f"- 初始资金：{portfolio.get('initial_capital')} 元")
    L.append(f"- 现金：{portfolio.get('cash')} 元")
    L.append(f"- 持仓市值：{portfolio.get('positions_value')} 元")
    L.append(f"- 已实现盈亏：{portfolio.get('realized_pnl')} 元")
    L.append(f"- 未实现盈亏：{portfolio.get('unrealized_pnl')} 元")
    L.append(f"- 总资产：{portfolio.get('total_assets')} 元（净收益率 {portfolio.get('net_return_pct')}%）")
    L.append(f"- 已确认成交笔数：{portfolio.get('trade_count')}")
    if not portfolio.get("positions"):
        L.append("- 当前空仓（无已确认成交记录）")
    L.append("")

    # 9. 智谱独立研究结论
    L.append("## 七、智谱独立研究结论")
    L.append("")
    L.append(f"（作者：{research.get('author')}，独立于 GPT-6 结论形成）")
    L.append("")
    for e in research.get("market_environment", []):
        L.append(f"- {e}")
    L.append("")
    L.append("**研究层风险提示**：")
    for r in research.get("risks", []) or ["无"]:
        L.append(f"- {r}")
    L.append("")
    L.append("**数据缺口**：")
    for g in research.get("data_gaps", []) or ["无"]:
        L.append(f"- {g}")
    L.append("")

    # 10. 独立风险审查
    L.append("## 八、独立风险审查（反方）")
    L.append("")
    L.append(f"（作者：{adversarial.get('author')}）")
    L.append("")
    L.append("| 检查项 | 方法 | 结果 | 判定 |")
    L.append("|---|---|---|---|")
    for c in adversarial.get("checks", []):
        L.append(f"| {md_escape(c.get('check'))} | {md_escape(c.get('method'))} "
                 f"| {md_escape((c.get('result') or '')[:70])} | {c.get('verdict')} |")
    L.append("")
    L.append(f"**反方总结**：失败 {adversarial.get('summary',{}).get('fail')} 项、"
             f"警告 {adversarial.get('summary',{}).get('warn')} 项。"
             f"{adversarial.get('summary',{}).get('conclusion')}")
    L.append("")

    # 11. 数据缺失与异常
    L.append("## 九、数据缺失与异常")
    L.append("")
    if snapshot.get("data_issues"):
        L.append("**快照采集问题**：")
        for i in snapshot["data_issues"]:
            L.append(f"- {i}")
    if market_list.get("issues"):
        L.append("**全市场列表问题**：")
        for i in market_list["issues"]:
            L.append(f"- {i}")
    q = quality.get("checks", {})
    ml = q.get("market_list", {})
    if ml.get("issues"):
        L.append("**市场列表质量**：")
        for i in ml["issues"][:5]:
            L.append(f"- {i}")
    L.append("")
    L.append("**已知数据源限制（须向 GPT-6 披露）**：")
    L.append("- 东财 push2 clist 全市场列表接口当前返回 502（域名级限流），已改用新浪源")
    L.append("- 腾讯 fqkline 日线接口在批量连续请求下触发 WAF 501，趋势计算已改用新浪日线")
    L.append("- 新浪日线为不复权价，除权日存在跳空，已由质量检查模块标记")
    L.append("- 央视新闻仅覆盖有播出的日期；周末无新数据时回溯至最近交易日")
    L.append("- 本轮未接入个股财报明细，候选缺少财务质量维度（属已知缺口，非数据错误）")
    L.append("")
    L.append("---")
    L.append("")
    L.append("*本报告由输入数据生成；数据来源、日期和前视偏差需单独核验，报告本身不证明其真实性。*")
    return "\n".join(L)


def default_rules():
    return {
        "账户规模": "1000 元人民币模拟账户",
        "交易权限": "仅模拟交易；不连接券商、不使用真实资金",
        "单笔约束": "一手价格 ≤ 900 元（扣除最低佣金与零头后仍可成交）",
        "成本假设": "以 fee_model 的适用配置为准；最低佣金等为压力测试假设，不是实际券商报价",
        "调仓频率": "低频优先（月度及以上），以实际回测与费用测算为准，不预设结论",
        "空仓许可": "允许长期空仓；禁止为完成交易次数而强制买卖",
        "决策流程": "来源明确的研究 → 反方审查 → 用户审核 → 用户确认模拟委托",
        "数据纪律": "所有结论须带来源与时间戳；数据缺失时明示，不得伪装实时行情",
    }


def export_all(snapshot, market_list, news, candidates_doc, research, adversarial, portfolio,
               quality, rules=None, out_dir=REPORTS):
    """导出全部报告文件"""
    rules = rules or default_rules()
    paths = {}
    paths["market_snapshot"] = save_json(snapshot, os.path.join(out_dir, "market_snapshot.json"))
    paths["news_events"] = save_json(news, os.path.join(out_dir, "news_events.json"))
    paths["stock_candidates"] = save_json(candidates_doc, os.path.join(out_dir, "stock_candidates.json"))
    paths["risk_review"] = save_json(adversarial, os.path.join(out_dir, "risk_review.json"))
    paths["research"] = save_json(research, os.path.join(out_dir, "research.json"))
    paths["portfolio"] = save_json(portfolio, os.path.join(out_dir, "portfolio.json"))
    md = build_decision_report(snapshot, market_list, news, candidates_doc["candidates"],
                               research, adversarial, portfolio, quality, rules)
    md_path = os.path.join(out_dir, "decision_report.md")
    with open(md_path, "w", encoding="utf-8") as f:
        f.write(md)
    paths["decision_report"] = md_path
    audit("report_exported", paths=paths, trade_date=snapshot.get("trade_date"))
    return paths


if __name__ == "__main__":
    snap = load_json(REPORTS + "/market_snapshot.json")
    ml = load_json(REPORTS + "/market_list.json")
    news = load_json(REPORTS + "/news_events.json")
    cands = load_json(REPORTS + "/stock_candidates.json")
    research = load_json(REPORTS + "/research.json")
    adv = load_json(REPORTS + "/risk_review.json")
    qual = load_json(REPORTS + "/quality_report.json")
    pf = ledger.portfolio()
    paths = export_all(snap, ml, news, cands, research, adv, pf, qual)
    print("exported:", paths)
