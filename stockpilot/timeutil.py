"""StockPilot 时间管理模块（第三阶段新增）。

任务书 5.1 要求严格区分八类时间：
1. 市场交易日期（trade_date）—— 数据所属的交易日
2. 行情实际时间（quote_time）—— 行情快照的具体时刻
3. 新闻首次发布时间（published_at）—— 事件的公开时间
4. 财报披露时间（disclosure_date）—— 财报实际公开的日期
5. 数据采集时间（collected_at）—— 系统抓到数据的时刻
6. 研究报告生成时间（generated_at）—— 报告产出时刻
7. GPT-6 决策时间（decided_at）—— 外部决策留痕时刻
8. 实际交易成交时间（filled_at）—— 券商确认成交时刻

统一规则：
- 一律使用 Asia/Shanghai 时区（A股交易时间基准），ISO 8601 带偏移量
- 输出格式：2026-10-18T13:17:22+08:00
- 内部一律用 aware datetime，禁止 naive datetime 混用
- 非交易日报告必须显示"最近有效交易日"与"数据陈旧程度"
"""
import datetime as dt

TZ_SHANGHAI = dt.timezone(dt.timedelta(hours=8), name="Asia/Shanghai")


def now():
    """当前时刻（Asia/Shanghai，aware）"""
    return dt.datetime.now(TZ_SHANGHAI)


def today():
    return now().date()


def iso(ts=None):
    """ISO 8601 带时区偏移；None → 当前时刻"""
    return (ts or now()).isoformat(timespec="seconds")


def to_shanghai(ts):
    """把任意 aware/naive 时间转为 Asia/Shanghai"""
    if ts.tzinfo is None:
        ts = ts.replace(tzinfo=TZ_SHANGHAI)
    return ts.astimezone(TZ_SHANGHAI)


def market_date(ts=None):
    """市场交易日期：以 Asia/Shanghai 的日历日为准。
    接受 date / datetime / None；统一返回 date。"""
    if ts is None:
        return now().date()
    if isinstance(ts, dt.datetime):
        return to_shanghai(ts).date()
    if isinstance(ts, dt.date):
        return ts
    return parse_iso(str(ts)).date() if parse_iso(str(ts)) else now().date()


def freshness(quote_time, collected_time=None, trade_date=None):
    """计算数据陈旧程度。

    返回 dict：
    - quote_time / collected_time / trade_date（ISO 带时区）
    - age_minutes: 行情时间距采集时间的分钟数
    - is_stale: 是否超过阈值
    - reason: 人类可读说明
    阈值：交易时段内 15 分钟，非交易时段以"最近交易日"表述。
    """
    collected = to_shanghai(collected_time) if collected_time else now()
    quote = to_shanghai(quote_time) if quote_time else None
    td = market_date(trade_date) if trade_date else None
    out = {
        "collected_at": iso(collected),
        "quote_time": iso(quote) if quote else None,
        "trade_date": td.isoformat() if td else None,
    }
    if not quote:
        out.update({"age_minutes": None, "is_stale": None, "reason": "无行情时间"})
        return out
    age = (collected - quote).total_seconds() / 60
    out["age_minutes"] = round(age, 1)
    # 判断采集时刻是否在交易时段（9:30-11:30, 13:00-15:00，周一至周五）
    def in_session(t):
        if t.weekday() >= 5:
            return False
        m = t.hour * 60 + t.minute
        return (9 * 60 + 30 <= m <= 11 * 60 + 30) or (13 * 60 <= m <= 15 * 60)
    if in_session(collected):
        threshold = 15
        out["threshold_minutes"] = threshold
        out["is_stale"] = age > threshold
        out["reason"] = ("交易时段内数据延迟超过阈值" if out["is_stale"]
                         else "交易时段内数据新鲜")
    else:
        # 非交易时段：数据应为最近交易日收盘，属于正常
        out["threshold_minutes"] = None
        out["is_stale"] = False
        out["reason"] = (f"非交易时段；数据来自 {quote.date().isoformat()} "
                         f"（距采集 {age/60:.1f} 小时），属正常收盘数据")
    return out


def trading_day_label(trade_date, collected=None):
    """给报告用的交易日期标签：明确区分'今天'与'最近有效交易日'"""
    collected = collected or now()
    td = market_date(trade_date) if trade_date else None
    if td is None:
        return "未知"
    if td == collected.date():
        return f"{td.isoformat()}（今日）"
    delta = (collected.date() - td).days
    return f"{td.isoformat()}（最近有效交易日，数据已陈旧 {delta} 天）"


def parse_iso(s):
    """解析 ISO 字符串（带或不带时区）；失败返回 None"""
    if not s:
        return None
    try:
        return dt.datetime.fromisoformat(s)
    except Exception:
        return None
