"""StockPilot 每日自动运行入口（任务调度外壳）。

设计原则（严格遵守阶段边界）：
- **不注册 Windows 任务计划，不写系统级自启动，不提权**。
  只提供"可以被外部调度器调用"的单次执行入口；是否真正定时，
  由用户显式选择（手动 / 任务计划 / 自己的调度器）。
- **不在非交易时段伪造实时数据**。非交易日或收盘后运行，数据会
  如实标注为最近有效交易日（沿用 timeutil.trading_day_label）。
- **可重复运行**。同一交易日重复执行不会重复入金、不会重复成交
  （幂等由 ledger 的 trade_uid 与 init_db 保证）。
- **失败不静默**。任何步骤失败都会记录到日志与 audit，并以非零
  退出码返回，便于外部调度器告警。

用法：
    # 手动执行一次完整流水线（采集→研究→反方→交接包）
    python -m stockpilot.scheduler run

    # 只检查"现在该不该跑"（交易时段判断），不实际采集
    python -m stockpilot.scheduler status

    # 打印建议的任务计划命令（不自动注册）
    python -m stockpilot.scheduler plan
"""
import datetime as dt
import os
import sys

from stockpilot import DATA, audit, iso, setup_logger
from stockpilot import timeutil as tu

log = setup_logger("scheduler", "scheduler.log")

# 交易时段（A 股）：09:30-11:30, 13:00-15:00，Asia/Shanghai
SESSIONS = (
    (dt.time(9, 30), dt.time(11, 30)),
    (dt.time(13, 0), dt.time(15, 0)),
)

# 建议的运行时点：收盘后 15:30（等数据落地），以及次日开盘前 09:00
SUGGESTED_RUN_TIMES = ("15:30", "09:00")


def is_trading_day(day=None):
    """是否为交易日（优先用交易日历，缺失时退化为周一至周五）。"""
    day = day or tu.today()
    cal = _load_trade_calendar()
    if cal:
        return day.isoformat() in cal
    return day.weekday() < 5


def _load_trade_calendar():
    """加载交易日历（存在则用，缺失返回 None，不联网）。"""
    import json
    p = os.path.join(DATA, "trade_calendar.json")
    if not os.path.exists(p):
        return None
    try:
        with open(p, "r", encoding="utf-8") as f:
            d = json.load(f)
        days = d.get("days") or d.get("trading_days") or []
        return set(days) if days else None
    except Exception as e:
        log.warning(f"交易日历读取失败：{e}")
        return None


def in_trading_session(now=None):
    """当前是否处于交易时段内。"""
    now = tu.to_shanghai(now) if now is not None else tu.now()
    if not is_trading_day(now.date()):
        return False
    t = now.time()
    return any(a <= t <= b for a, b in SESSIONS)


def should_run(now=None, force=False):
    """判断现在是否适合运行一次完整采集。

    返回 (bool, reason)。
    - 非交易日：可运行，但数据为"最近有效交易日"，不伪造实时
    - 交易时段内：建议不要跑完整流水线（数据仍在变动）
    - 收盘后：最佳时点
    """
    now = tu.to_shanghai(now) if now is not None else tu.now()
    if force:
        return True, "强制运行（--force）"
    day = now.date()
    if not is_trading_day(day):
        return True, (f"{day} 非交易日，运行将使用最近有效交易日数据"
                      f"（{tu.trading_day_label(day)}），不伪造实时行情")
    if now.time() <= dt.time(9, 0):
        return True, f"当前 {now.strftime('%H:%M')} 为盘前准备窗口；不代表已有当日收盘数据"
    if now.time() < dt.time(15, 30):
        return False, (f"当前 {now.strftime('%H:%M')} 尚未到完整日线采集窗口，"
                       "包括交易时段与午间休市；建议收盘后 15:30 再运行")
    return True, f"当前 {now.strftime('%H:%M')} 已到收盘后采集窗口；仍需检查数据日期"



def run_once(force=False, skip_scan=False):
    """执行一次完整流水线（复用 pipeline_v4，不重写已有逻辑）。"""
    ok, reason = should_run(force=force)
    log.info(f"调度判断：{'运行' if ok else '跳过'} — {reason}")
    audit("scheduler_run_decision", run=ok, reason=reason, at=iso())
    if not ok:
        print(f"[跳过] {reason}")
        return {"ran": False, "reason": reason}

    print(f"[运行] {reason}")
    from stockpilot import pipeline_v4
    try:
        result = pipeline_v4.main(skip_scan=skip_scan)
        audit("scheduler_run_complete", ok=True, at=iso())
        return {"ran": True, "reason": reason, "result": result}
    except Exception as e:
        log.exception("流水线执行失败")
        audit("scheduler_run_failed", error=str(e), at=iso())
        print(f"[失败] {e}", file=sys.stderr)
        raise


def print_plan():
    """打印供用户自行注册的调度命令（不自动注册任何系统任务）。"""
    py = sys.executable
    root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    print("StockPilot 调度建议（本工具不会自动注册任何系统任务）")
    print("=" * 62)
    print(f"项目根目录 : {root}")
    print(f"解释器     : {py}")
    print(f"建议时点   : {', '.join(SUGGESTED_RUN_TIMES)}（收盘后 / 盘前）")
    print()
    print("1) 手动运行一次：")
    print(f'   cd /d "{root}"')
    print(f'   "{py}" -m stockpilot.scheduler run')
    print()
    print("2) 若希望 Windows 任务计划定时执行，请自行在“任务计划程序”中")
    print("   新建任务，操作填：")
    print(f'     程序: "{py}"')
    print(f'     参数: -m stockpilot.scheduler run')
    print(f'     起始于: "{root}"')
    print("   —— 需要管理员权限或写入系统级任务，本工具不代为执行。")
    print()
    print("3) 若用 Python 调度库（已安装 schedule）自行包一层循环：")
    print("     schedule.every().day.at('15:30').do(lambda: run_once())")
    print()
    print("注意：非交易日/交易时段内运行时，脚本会自行判断并跳过或标注，")
    print("      不会把旧数据伪装成实时行情。")


def print_status():
    now = tu.now()
    ok, reason = should_run(now=now)
    print(f"当前时间     : {now.strftime('%Y-%m-%d %H:%M:%S')}")
    print(f"是否交易日   : {is_trading_day(now.date())}")
    print(f"是否交易时段 : {in_trading_session(now)}")
    print(f"交易日期标签 : {tu.trading_day_label(now.date())}")
    print(f"建议         : {'可以运行' if ok else '建议暂缓'} — {reason}")


def main(argv=None):
    argv = argv if argv is not None else sys.argv[1:]
    cmd = argv[0] if argv else "status"
    force = "--force" in argv
    skip_scan = "--skip-scan" in argv

    if cmd == "run":
        res = run_once(force=force, skip_scan=skip_scan)
        return 0 if res.get("ran") else 0
    if cmd == "status":
        print_status()
        return 0
    if cmd == "plan":
        print_plan()
        return 0
    print(__doc__)
    return 2


if __name__ == "__main__":
    sys.exit(main())
